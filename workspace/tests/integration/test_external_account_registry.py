# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Generic external accounts need no bridge or provider processing."""

import uuid as sys_uuid


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
    assert body["settings"] == spec["settings"]
    assert "credential_present" not in body
    assert "capabilities" not in body
    path = root + account_uuid
    read = api.get(path, permissions=permissions)
    assert read.status_code == 200, read.text
    assert (
        api.get(path, user=str(sys_uuid.uuid4()), permissions=permissions).status_code
        == 404
    )
    updated = api.put(
        path,
        json={"settings": {"label": "Renamed"}},
        headers={"If-Match": read.headers["ETag"]},
        permissions=permissions,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["settings"] == {"label": "Renamed"}
    assert updated.headers["ETag"] != read.headers["ETag"]
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
