# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import contextlib
import types
import uuid as sys_uuid

from restalchemy.api import routes as ra_routes

from workspace.messenger_api.api import controllers
from workspace.messenger_api.api import routes
from workspace.messenger_api.api import store as api_store


def _controller(controller_class, project_uuid, user_uuid):
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_uuid,
            user_uuid=user_uuid,
        )
    )
    return controller_class(request)


class FolderStore:
    def __init__(self):
        self.calls = []

    def create_resource(self, resource, values):
        self.calls.append(("create", resource, values))
        return values

    def update_resource(self, resource, resource_uuid, values):
        self.calls.append(("update", resource, resource_uuid, values))
        return {"uuid": resource_uuid, **values}

    def delete_resource(self, resource, resource_uuid):
        self.calls.append(("delete", resource, resource_uuid))

    def perform_action(self, resource, resource_uuid, action, values):
        self.calls.append(("action", resource, resource_uuid, action, values))
        return {"uuid": resource_uuid}


def test_folder_routes_keep_update_and_delete():
    assert ra_routes.UPDATE in routes.FolderRoute.__allow_methods__
    assert ra_routes.DELETE in routes.FolderRoute.__allow_methods__
    assert ra_routes.DELETE in routes.FolderItemRoute.__allow_methods__


def test_folder_and_item_mutations_use_scoped_store():
    project_uuid = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    folder_uuid = sys_uuid.uuid4()
    item_uuid = sys_uuid.uuid4()
    store = FolderStore()
    scopes = []

    def factory(project_id, actor_uuid):
        scopes.append((project_id, actor_uuid))
        return contextlib.nullcontext(store)

    api_store.configure_store_factory(factory)
    try:
        folder = _controller(controllers.FolderController, project_uuid, user_uuid)
        item = _controller(controllers.FolderItemController, project_uuid, user_uuid)
        folder.update(folder_uuid, title="Archive")
        folder.delete(folder_uuid)
        item.create(
            uuid=item_uuid,
            folder_uuid=folder_uuid,
            stream_uuid=sys_uuid.uuid4(),
            chat_type="stream",
        )
        item.delete(item_uuid)
        controllers.FolderItemController.pin._post(
            self=item,
            resource={"uuid": item_uuid},
        )
        controllers.FolderItemController.unpin._post(
            self=item,
            resource={"uuid": item_uuid},
        )
    finally:
        api_store.reset_store_factory()

    assert scopes == [(project_uuid, user_uuid)] * 6
    assert [call[0:2] for call in store.calls] == [
        ("update", "folders"),
        ("delete", "folders"),
        ("create", "folder_items"),
        ("delete", "folder_items"),
        ("action", "folder_items"),
        ("action", "folder_items"),
    ]
    assert store.calls[4][3] == "pin"
    assert store.calls[5][3] == "unpin"
