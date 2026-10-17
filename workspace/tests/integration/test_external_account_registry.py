# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""External account registration and bridge initialization."""

import uuid as sys_uuid

from workspace.bridges.zulip.dm import models as zulip_models
from workspace.messenger_api import application_services


def test_zulip_account_creation_queues_connection_initialization(api, db):
    root = "/v1/messenger/external_accounts/"
    account_uuid = str(sys_uuid.uuid4())
    created = api.post(
        root,
        json={"uuid": account_uuid, "provider": "zulip", "settings": {}},
        permissions=("workspace.external_account.create",),
    )

    assert created.status_code == 201, created.text
    tasks = db.execute(
        "SELECT project_id, priority, status, content FROM bridge_zulip_v5_tasks WHERE external_account_uuid=%s",
        (account_uuid,),
    ).fetchall()
    assert tasks == [
        (
            sys_uuid.UUID(api.project_id),
            zulip_models.TASK_PRIORITY_INITIALIZE_CONNECTION,
            "new",
            {
                "kind": "initialize_connection",
            },
        )
    ]
    assert (
        api.delete(
            root + account_uuid,
            permissions=("workspace.external_account.delete",),
        ).status_code
        == 204
    )


def test_zulip_account_update_resets_connection_and_queues_a_new_task(api, db):
    root = "/v1/messenger/external_accounts/"
    account_uuid = str(sys_uuid.uuid4())
    path = root + account_uuid
    permissions = tuple(
        f"workspace.external_account.{action}"
        for action in ("create", "update", "delete")
    )
    created = api.post(
        root,
        json={
            "uuid": account_uuid,
            "provider": "zulip",
            "settings": {"api_key": "old-key"},
        },
        permissions=permissions,
    )
    assert created.status_code == 201, created.text
    db.execute(
        "UPDATE m_external_accounts_v2 SET status='error', "
        "error_description='Authentication failed' WHERE uuid=%s",
        (account_uuid,),
    )
    old_task_uuid = db.execute(
        "UPDATE bridge_zulip_v5_tasks SET status='error' "
        "WHERE external_account_uuid=%s RETURNING uuid",
        (account_uuid,),
    ).fetchone()[0]
    update = {
        "settings": {"api_key": "new-key"},
        "status": "error",
        "error_description": "Authentication failed",
    }
    assert (
        api.put(
            path,
            json=update,
            user=str(sys_uuid.uuid4()),
            permissions=permissions,
        ).status_code
        == 404
    )

    updated = api.put(path, json=update, permissions=permissions)

    assert updated.status_code == 200, updated.text
    assert updated.json()["status"] == "new"
    assert updated.json().get("error_description") is None
    assert updated.json()["settings"] == {"api_key": "new-key"}
    assert updated.json()["project_id"] == api.project_id
    events = db.execute(
        "SELECT project_id, user_uuid, payload FROM m_workspace_events "
        "WHERE object_type='external_account' AND action='updated' "
        "AND payload->>'uuid'=%s",
        (account_uuid,),
    ).fetchall()
    assert len(events) == 1
    project_id, user_uuid, payload = events[0]
    assert str(project_id) == str(api.project_id)
    assert str(user_uuid) == str(api.user_uuid)
    assert payload["snapshot"]["status"] == "new"
    assert payload["snapshot"]["settings"] == {"api_key": "new-key"}
    tasks = db.execute(
        "SELECT uuid, priority, status, content, project_id FROM bridge_zulip_v5_tasks "
        "WHERE external_account_uuid=%s",
        (account_uuid,),
    ).fetchall()
    assert len(tasks) == 2
    assert next(task[2] for task in tasks if task[0] == old_task_uuid) == "error"
    assert [task[1:] for task in tasks if task[0] != old_task_uuid] == [
        (
            zulip_models.TASK_PRIORITY_INITIALIZE_CONNECTION,
            "new",
            {
                "kind": "initialize_connection",
            },
            sys_uuid.UUID(api.project_id),
        )
    ]
    assert api.delete(path, permissions=permissions).status_code == 204


def test_accounts_and_tasks_are_scoped_to_the_request_project(api, db):
    root = "/v1/messenger/external_accounts/"
    permissions = tuple(
        f"workspace.external_account.{action}"
        for action in ("create", "read", "update", "delete")
    )
    other_project = str(sys_uuid.uuid4())
    account_uuid = str(sys_uuid.uuid4())
    created = api.post(
        root,
        json={
            "uuid": account_uuid,
            "project_id": other_project,
            "provider": "zulip",
            "settings": {},
        },
        permissions=permissions,
    )
    assert created.status_code == 201, created.text
    assert created.json()["project_id"] == api.project_id
    other_account_uuid = str(sys_uuid.uuid4())
    other = api.post(
        root,
        json={"uuid": other_account_uuid, "provider": "zulip", "settings": {}},
        project=other_project,
        permissions=permissions,
    )
    assert other.status_code == 201, other.text
    assert other.json()["project_id"] == other_project
    for method in ("get", "put", "delete"):
        response = getattr(api, method)(
            root + account_uuid,
            project=other_project,
            permissions=permissions,
            **({"json": {"settings": {}}} if method == "put" else {}),
        )
        assert response.status_code == 404, response.text
    listed = api.get(root, permissions=permissions)
    assert [account["uuid"] for account in listed.json()] == [account_uuid]
    filtered = api.get(
        root, params={"project_id": other_project}, permissions=permissions
    )
    assert [account["uuid"] for account in filtered.json()] == [account_uuid]

    updated = api.put(
        root + account_uuid, json={"settings": {}}, permissions=permissions
    )
    assert updated.status_code == 200, updated.text
    assert db.execute(
        "SELECT project_id, status FROM bridge_zulip_v5_tasks "
        "WHERE external_account_uuid=%s",
        (other_account_uuid,),
    ).fetchall() == [(sys_uuid.UUID(other_project), "new")]
    assert api.delete(root + account_uuid, permissions=permissions).status_code == 204
    assert (
        api.delete(
            root + other_account_uuid, project=other_project, permissions=permissions
        ).status_code
        == 204
    )


def test_account_update_and_new_task_roll_back_when_event_creation_fails(
    api, db, monkeypatch
):
    root = "/v1/messenger/external_accounts/"
    account_uuid = str(sys_uuid.uuid4())
    path = root + account_uuid
    permissions = tuple(
        f"workspace.external_account.{action}"
        for action in ("create", "update", "delete")
    )
    created = api.post(
        root,
        json={
            "uuid": account_uuid,
            "provider": "zulip",
            "settings": {"api_key": "old-key"},
        },
        permissions=permissions,
    )
    assert created.status_code == 201, created.text
    db.execute(
        "UPDATE m_external_accounts_v2 SET status='error', "
        "error_description='Authentication failed' WHERE uuid=%s",
        (account_uuid,),
    )

    def fail_event(*args, **kwargs):
        raise RuntimeError("Event creation failed")

    with monkeypatch.context() as patch:
        patch.setattr(
            application_services.messenger_events,
            "create_external_resource_event",
            fail_event,
        )
        updated = api.put(
            path,
            json={"settings": {"api_key": "new-key"}},
            permissions=permissions,
        )

    assert updated.status_code == 500, updated.text
    assert db.execute(
        "SELECT settings, status, error_description "
        "FROM m_external_accounts_v2 WHERE uuid=%s",
        (account_uuid,),
    ).fetchone() == ({"api_key": "old-key"}, "error", "Authentication failed")
    assert (
        db.execute(
            "SELECT COUNT(*) FROM bridge_zulip_v5_tasks WHERE external_account_uuid=%s",
            (account_uuid,),
        ).fetchone()[0]
        == 1
    )
    assert api.delete(path, permissions=permissions).status_code == 204


def test_account_and_initialization_task_roll_back_when_event_creation_fails(
    api, db, monkeypatch
):
    account_uuid = str(sys_uuid.uuid4())

    def fail_event(*args, **kwargs):
        raise RuntimeError("Event creation failed")

    monkeypatch.setattr(
        application_services.messenger_events,
        "create_external_resource_event",
        fail_event,
    )
    created = api.post(
        "/v1/messenger/external_accounts/",
        json={"uuid": account_uuid, "provider": "zulip", "settings": {}},
        permissions=("workspace.external_account.create",),
    )

    assert created.status_code == 500, created.text
    assert (
        db.execute(
            "SELECT uuid FROM m_external_accounts_v2 WHERE uuid=%s", (account_uuid,)
        ).fetchall()
        == []
    )
    assert (
        db.execute(
            "SELECT uuid FROM bridge_zulip_v5_tasks WHERE external_account_uuid=%s",
            (account_uuid,),
        ).fetchall()
        == []
    )


def test_generic_account_crud_is_owner_scoped(api):
    root = "/v1/messenger/external_accounts/"
    account_uuid = str(sys_uuid.uuid4())
    spec = {
        "uuid": account_uuid,
        "provider": "example",
        "settings": {"label": "Account"},
    }
    permissions = tuple(
        f"workspace.external_account.{action}"
        for action in ("read", "create", "update", "delete")
    )
    created = api.post(root, json=spec, permissions=permissions)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["provider"] == "example"
    assert body["status"] == "new"
    assert body.get("error_description") is None
    assert body["settings"] == spec["settings"]
    assert "credential_present" not in body
    assert "capabilities" not in body
    assert "revision" not in body
    assert "ETag" not in created.headers
    path = root + account_uuid
    read = api.get(path, permissions=permissions)
    assert read.status_code == 200, read.text
    assert "revision" not in read.json()
    assert "ETag" not in read.headers
    assert (
        api.get(path, user=str(sys_uuid.uuid4()), permissions=permissions).status_code
        == 404
    )
    updated = api.put(
        path,
        json={"settings": {"label": "Renamed"}, "status": "fast_backfill"},
        permissions=permissions,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["settings"] == {"label": "Renamed"}
    assert updated.json()["status"] == "fast_backfill"
    assert "revision" not in updated.json()
    assert "ETag" not in updated.headers
    failed = api.put(
        path,
        json={"status": "error", "error_description": "Authentication failed"},
        headers={"If-Match": '"outdated"'},
        permissions=permissions,
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "error"
    assert failed.json()["error_description"] == "Authentication failed"
    recovered = api.put(
        path,
        json={"status": "active", "error_description": None},
        permissions=permissions,
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "active"
    assert recovered.json().get("error_description") is None
    assert api.delete(path, permissions=permissions).status_code == 204
    assert api.get(path, permissions=permissions).status_code == 404


def test_removed_processing_routes_are_absent(api):
    for resource in (
        "external_chats",
        "external_operations",
        "external_bridge_instances",
        "external_provider_policies",
        "external_provider_health",
    ):
        assert api.get(f"/v1/{resource}/").status_code == 501
