# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import contextlib
import io
import types
import uuid as sys_uuid
from unittest import mock

import pytest

from workspace.messenger_api.api import controllers
from workspace.messenger_api.api import store as api_store


def test_get_user_uses_request_project_and_actor_store_scope():
    project_uuid = sys_uuid.uuid4()
    actor_uuid = sys_uuid.uuid4()
    target_uuid = sys_uuid.uuid4()
    scopes = []

    class Store:
        def sync_iam_identity(self, values):
            raise AssertionError("another user's identity must not be refreshed")

        def get_resource(self, resource, resource_uuid):
            assert resource == "users"
            assert resource_uuid == target_uuid
            return {"uuid": resource_uuid}

    def factory(project_id, user_uuid):
        scopes.append((project_id, user_uuid))
        return contextlib.nullcontext(Store())

    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_uuid,
            user_uuid=actor_uuid,
        )
    )
    api_store.configure_store_factory(factory)
    try:
        result = controllers.WorkspaceUserController(request).get(target_uuid)
    finally:
        api_store.reset_store_factory()

    assert result == {"uuid": target_uuid}
    assert scopes == [(project_uuid, actor_uuid)]


def test_get_current_user_materializes_iam_identity_before_reading_projection():
    project_uuid = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    calls = []
    iam_user = types.SimpleNamespace(
        name="cassi",
        first_name="Cassandra",
        last_name="Volkova",
        email="cassi@exordos.com",
    )

    class Store:
        def sync_iam_identity(self, values):
            calls.append(("sync", values))

        def get_resource(self, resource, resource_uuid):
            calls.append(("get", resource, resource_uuid))
            return {"uuid": resource_uuid, "username": "cassi"}

    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_uuid,
            user_uuid=user_uuid,
            iam_context=types.SimpleNamespace(
                get_introspection_info=lambda: types.SimpleNamespace(
                    user_info=iam_user,
                )
            ),
        )
    )
    api_store.configure_store_factory(
        lambda project_id, actor_uuid: contextlib.nullcontext(Store())
    )
    try:
        result = controllers.WorkspaceUserController(request).get(user_uuid)
    finally:
        api_store.reset_store_factory()

    assert result == {"uuid": user_uuid, "username": "cassi"}
    assert calls == [
        (
            "sync",
            {
                "user_uuid": user_uuid,
                "username": "cassi",
                "first_name": "Cassandra",
                "last_name": "Volkova",
                "email": "cassi@exordos.com",
            },
        ),
        ("get", "users", user_uuid),
    ]


def test_me_returns_current_iam_user_profile():
    project_uuid = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    calls = []
    iam_user = types.SimpleNamespace(
        name="cassi",
        first_name="Cassandra",
        last_name="Volkova",
        email="cassi@exordos.com",
    )

    class Store:
        def sync_iam_identity(self, values):
            calls.append(("sync", values))

        def get_resource(self, resource, resource_uuid):
            calls.append(("get", resource, resource_uuid))
            return {
                "uuid": resource_uuid,
                "username": "cassi",
                "avatar": "urn:gravatar:" + "a" * 32,
            }

    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_uuid,
            user_uuid=user_uuid,
            iam_context=types.SimpleNamespace(
                get_introspection_info=lambda: types.SimpleNamespace(
                    user_info=iam_user,
                )
            ),
        )
    )
    scopes = []

    def factory(project_id, actor_uuid):
        scopes.append((project_id, actor_uuid))
        return contextlib.nullcontext(Store())

    api_store.configure_store_factory(factory)
    try:
        result = controllers.MeController(request).filter({}, order_by=None)
    finally:
        api_store.reset_store_factory()

    assert result == {
        "uuid": user_uuid,
        "username": "cassi",
        "avatar": "urn:gravatar:" + "a" * 32,
    }
    assert scopes == [(project_uuid, user_uuid)]
    assert calls == [
        (
            "sync",
            {
                "user_uuid": user_uuid,
                "username": "cassi",
                "first_name": "Cassandra",
                "last_name": "Volkova",
                "email": "cassi@exordos.com",
            },
        ),
        ("get", "users", user_uuid),
    ]


def test_upload_own_avatar_stores_public_object_and_updates_user():
    project_uuid = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    calls = []
    storage_info = controllers.file_storage.WorkspaceFileStorageInfo(
        storage_type="s3",
        storage_id="workspace-files",
        storage_object_id="avatar/key",
    )

    class Store:
        def perform_action(self, resource, resource_uuid, action, values):
            calls.append((resource, resource_uuid, action, values))
            return {
                "uuid": resource_uuid,
                "avatar": f"urn:image:{values['uuid']}",
            }

    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_uuid,
            user_uuid=user_uuid,
        )
    )
    api_store.configure_store_factory(
        lambda project_id, actor_uuid: contextlib.nullcontext(Store())
    )
    try:
        with (
            mock.patch.object(
                controllers.file_storage,
                "save_workspace_file",
                return_value=storage_info,
            ),
            mock.patch.object(
                controllers.file_storage,
                "save_workspace_file_metadata",
            ) as save_metadata,
        ):
            controller = controllers.WorkspaceUserController(request)
            result = controller.avatar_upload._post(
                controller,
                {"uuid": user_uuid, "avatar": "urn:gravatar:" + "a" * 32},
                multipart=True,
                parts={
                    "file": types.SimpleNamespace(
                        file=io.BytesIO(b"\x89PNG\r\n\x1a\nimage"),
                        filename="avatar.png",
                        type="image/png",
                    )
                },
            )
    finally:
        api_store.reset_store_factory()

    assert result["avatar"].startswith("urn:image:")
    metadata = save_metadata.call_args.args[0]
    assert metadata.acl_mode == "public"
    assert metadata.stream_uuid is None
    assert metadata.owner_uuid == user_uuid
    assert calls[0][2] == "avatar_upload"
    assert "stream_uuid" not in calls[0][3]


def test_upload_avatar_rejects_non_image_before_storage():
    user_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=sys_uuid.uuid4(),
            user_uuid=user_uuid,
        )
    )
    with mock.patch.object(
        controllers.file_storage,
        "save_workspace_file",
    ) as save_file:
        with pytest.raises(Exception):
            controller = controllers.WorkspaceUserController(request)
            controller.avatar_upload._post(
                controller,
                {"uuid": user_uuid, "avatar": "urn:gravatar:" + "a" * 32},
                multipart=True,
                parts={
                    "file": types.SimpleNamespace(
                        file=io.BytesIO(b"not an image"),
                        filename="avatar.png",
                        type="image/png",
                    )
                },
            )
    save_file.assert_not_called()
