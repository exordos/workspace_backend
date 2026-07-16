# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Authorization and route policy tests for the Messenger store boundary."""

import contextlib
import hashlib
import io
import types
import uuid as sys_uuid
from unittest import mock

import pytest
from gcl_iam.api import controllers as iam_controllers
from restalchemy.api import routes as ra_routes

from workspace.messenger_api.api import controllers
from workspace.messenger_api.api import routes
from workspace.messenger_api.api import sql_store
from workspace.messenger_api.api import store as api_store
from workspace.messenger_mail import repository


PROJECT_UUID = sys_uuid.UUID("10000000-0000-0000-0000-000000000001")
USER_UUID = sys_uuid.UUID("20000000-0000-0000-0000-000000000002")
PEER_UUID = sys_uuid.UUID("30000000-0000-0000-0000-000000000003")


class RecordingStore:
    def __init__(self):
        self.calls = []

    def filter_resources(self, resource, filters, order_by=None):
        self.calls.append(("filter", resource, filters, order_by))
        return []

    def get_resource(self, resource, resource_uuid):
        self.calls.append(("get", resource, resource_uuid))
        return {
            "uuid": resource_uuid,
            "name": "example.txt",
            "content_type": "text/plain",
            "hash": "a" * 64,
            "storage_type": "s3",
            "storage_object_id": "key",
        }

    def create_resource(self, resource, values):
        self.calls.append(("create", resource, values))
        return values.copy()

    def update_resource(self, resource, resource_uuid, values):
        self.calls.append(("update", resource, resource_uuid, values))
        return {"uuid": resource_uuid, **values}

    def delete_resource(self, resource, resource_uuid):
        self.calls.append(("delete", resource, resource_uuid))
        return {"uuid": resource_uuid}

    def perform_action(self, resource, resource_uuid, action, values):
        self.calls.append(("action", resource, resource_uuid, action, values))
        return {"uuid": resource_uuid, **values}

    def create_message(self, values):
        self.calls.append(("create_message", values))
        return values.copy()

    def update_message(self, message_uuid, values):
        self.calls.append(("update_message", message_uuid, values))
        return {"uuid": message_uuid, **values}

    def delete_message(self, message_uuid):
        self.calls.append(("delete_message", message_uuid))
        return {"uuid": message_uuid}

    def events_after(self, filters, order_by=None):
        self.calls.append(("events", filters, order_by))
        return []

    def current_epoch(self):
        self.calls.append(("epoch",))
        return 0


@pytest.fixture
def store_factory():
    store = RecordingStore()
    opened_scopes = []

    def factory(project_uuid, user_uuid):
        opened_scopes.append((project_uuid, user_uuid))
        return contextlib.nullcontext(store)

    api_store.configure_store_factory(factory)
    try:
        yield store, opened_scopes
    finally:
        api_store.reset_store_factory()


def _controller(controller_class):
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=PROJECT_UUID,
            user_uuid=USER_UUID,
        )
    )
    return controller_class(request)


def _invoke_action(controller, action, resource_uuid, **values):
    descriptor = getattr(type(controller), action)
    return descriptor._post(
        self=controller,
        resource={"uuid": resource_uuid},
        **values,
    )


def test_controller_uses_local_store_scope_not_iam_policy_controller():
    assert iam_controllers.PolicyBasedController not in (
        controllers.StoreResourceController.mro()
    )


def test_create_overrides_caller_project_and_user_with_request_scope(store_factory):
    store, opened_scopes = store_factory
    topic_uuid = sys_uuid.uuid4()
    controller = _controller(controllers.WorkspaceStreamTopicController)

    result = controller.create(
        uuid=topic_uuid,
        project_id=sys_uuid.uuid4(),
        user_uuid=sys_uuid.uuid4(),
        stream_uuid=sys_uuid.uuid4(),
        name="planning",
    )

    assert opened_scopes == [(PROJECT_UUID, USER_UUID)]
    values = store.calls[0][2]
    assert store.calls[0][0:2] == ("create", "stream_topics")
    assert values["project_id"] == PROJECT_UUID
    assert values["user_uuid"] == USER_UUID
    assert result == values


@pytest.mark.parametrize(
    ("controller_class", "resource", "create_values"),
    (
        (
            controllers.FolderController,
            "folders",
            {"name": "Important"},
        ),
        (
            controllers.WorkspaceStreamController,
            "streams",
            {"name": "Core", "description": "Team"},
        ),
        (
            controllers.WorkspaceStreamTopicController,
            "stream_topics",
            {"stream_uuid": sys_uuid.uuid4(), "name": "Planning"},
        ),
        (
            controllers.WorkspaceMessageReactionController,
            "message_reactions",
            {"message_uuid": sys_uuid.uuid4(), "emoji_name": "heart"},
        ),
    ),
)
def test_generic_mutations_stay_inside_current_context_scope(
    store_factory,
    controller_class,
    resource,
    create_values,
):
    store, opened_scopes = store_factory
    controller = _controller(controller_class)
    resource_uuid = sys_uuid.uuid4()

    controller.create(
        uuid=resource_uuid,
        project_id=sys_uuid.uuid4(),
        user_uuid=sys_uuid.uuid4(),
        **create_values,
    )
    controller.update(
        resource_uuid,
        project_id=sys_uuid.uuid4(),
        user_uuid=sys_uuid.uuid4(),
        name="updated",
    )
    controller.delete(resource_uuid)

    assert opened_scopes == [(PROJECT_UUID, USER_UUID)] * 3
    assert [call[0:2] for call in store.calls] == [
        ("create", resource),
        ("update", resource),
        ("delete", resource),
    ]
    assert store.calls[1][3] == {"name": "updated"}


def test_message_mutations_use_current_scope_and_dedicated_store_operations(
    store_factory,
):
    store, opened_scopes = store_factory
    controller = _controller(controllers.WorkspaceMessageController)
    message_uuid = sys_uuid.uuid4()

    controller.create(
        uuid=message_uuid,
        project_id=sys_uuid.uuid4(),
        user_uuid=sys_uuid.uuid4(),
        stream_uuid=sys_uuid.uuid4(),
        topic_uuid=sys_uuid.uuid4(),
        payload={"kind": "markdown", "content": "hello"},
    )
    controller.update(
        message_uuid,
        project_id=sys_uuid.uuid4(),
        user_uuid=sys_uuid.uuid4(),
        payload={"kind": "markdown", "content": "edited"},
    )
    controller.delete(message_uuid)

    assert opened_scopes == [(PROJECT_UUID, USER_UUID)] * 3
    assert [call[0] for call in store.calls] == [
        "create_message",
        "update_message",
        "delete_message",
    ]
    assert store.calls[0][1]["project_id"] == PROJECT_UUID
    assert store.calls[0][1]["user_uuid"] == USER_UUID
    assert store.calls[1][2] == {"payload": {"kind": "markdown", "content": "edited"}}


@pytest.mark.parametrize(
    ("controller_class", "resource", "action", "values"),
    (
        (controllers.FolderItemController, "folder_items", "pin", {}),
        (controllers.FolderItemController, "folder_items", "unpin", {}),
        (controllers.WorkspaceStreamController, "streams", "archive", {}),
        (controllers.WorkspaceStreamController, "streams", "unarchive", {}),
        (
            controllers.WorkspaceStreamController,
            "streams",
            "notifications",
            {"notification_mode": "mentions_only"},
        ),
        (controllers.WorkspaceStreamController, "streams", "read", {}),
        (
            controllers.WorkspaceStreamBindingController,
            "stream_bindings",
            "add_users",
            {"member": [PEER_UUID]},
        ),
        (
            controllers.WorkspaceStreamTopicController,
            "stream_topics",
            "toggle_done",
            {},
        ),
        (
            controllers.WorkspaceStreamTopicController,
            "stream_topics",
            "notifications",
            {"notification_mode": "follow"},
        ),
        (
            controllers.WorkspaceStreamTopicController,
            "stream_topics",
            "set_default",
            {},
        ),
        (
            controllers.WorkspaceStreamTopicController,
            "stream_topics",
            "read",
            {},
        ),
        (controllers.WorkspaceMessageController, "messages", "read", {}),
        (
            controllers.WorkspaceMessageController,
            "messages",
            "read_up_to",
            {},
        ),
        (
            controllers.WorkspaceUserController,
            "users",
            "presence",
            {"status": "available"},
        ),
    ),
)
def test_actions_use_current_scope_and_store_resource(
    store_factory,
    controller_class,
    resource,
    action,
    values,
):
    store, opened_scopes = store_factory
    resource_uuid = sys_uuid.uuid4()

    _invoke_action(_controller(controller_class), action, resource_uuid, **values)

    assert opened_scopes == [(PROJECT_UUID, USER_UUID)]
    assert store.calls == [
        ("action", resource, resource_uuid, action, values),
    ]


def test_direct_stream_uses_project_scoped_pair_uuid_and_rejects_self(store_factory):
    store, _opened_scopes = store_factory
    controller = _controller(controllers.WorkspaceStreamController)

    result = controller.create(name="Direct", direct_user_uuid=PEER_UUID)

    expected_uuid = repository.deterministic_dm_uuid(
        PROJECT_UUID,
        USER_UUID,
        PEER_UUID,
    )
    assert result["uuid"] == expected_uuid
    assert result["private"] is True
    assert store.calls[0][0:2] == ("create", "streams")
    with pytest.raises(Exception):
        controller.create(name="Self", direct_user_uuid=USER_UUID)


def test_stream_resource_hides_internal_private_index():
    resource = controllers.WorkspaceStreamController.__resource__

    assert resource.is_public_field("private_index") is False
    assert resource.is_public_field("name") is True


def test_route_permissions_and_action_owners_are_preserved():
    assert routes.ApiEndpointRoute.files is routes.WorkspaceFileRoute
    assert ra_routes.DELETE in routes.WorkspaceStreamRoute.__allow_methods__
    assert ra_routes.UPDATE in routes.WorkspaceStreamRoute.__allow_methods__
    assert ra_routes.UPDATE in routes.WorkspaceMessageRoute.__allow_methods__
    assert ra_routes.DELETE in routes.WorkspaceMessageRoute.__allow_methods__
    assert set(routes.WorkspaceMessageReactionRoute.__allow_methods__) == {
        ra_routes.CREATE,
        ra_routes.FILTER,
        ra_routes.GET,
        ra_routes.UPDATE,
        ra_routes.DELETE,
    }
    assert ra_routes.CREATE not in (
        routes.WorkspaceStreamBindingRoute.__allow_methods__
    )
    assert set(routes.WorkspaceFileRoute.__allow_methods__) == {
        ra_routes.CREATE,
        ra_routes.FILTER,
        ra_routes.GET,
        ra_routes.UPDATE,
        ra_routes.DELETE,
    }
    expected_action_owners = {
        routes.WorkspaceStreamBindingsAction: (
            controllers.WorkspaceStreamBindingController
        ),
        routes.WorkspaceStreamArchiveAction: controllers.WorkspaceStreamController,
        routes.WorkspaceStreamUnarchiveAction: (controllers.WorkspaceStreamController),
        routes.WorkspaceStreamNotificationsAction: (
            controllers.WorkspaceStreamController
        ),
        routes.WorkspaceStreamReadAction: controllers.WorkspaceStreamController,
        routes.WorkspaceStreamTopicNotificationsAction: (
            controllers.WorkspaceStreamTopicController
        ),
        routes.WorkspaceStreamTopicReadAction: (
            controllers.WorkspaceStreamTopicController
        ),
        routes.WorkspaceMessageReadAction: controllers.WorkspaceMessageController,
        routes.WorkspaceMessageReadUpToAction: (controllers.WorkspaceMessageController),
        routes.WorkspaceFileDownloadAction: controllers.WorkspaceFileController,
    }
    for action_route, owner in expected_action_owners.items():
        assert action_route.__controller__ is owner
    assert not routes.WorkspaceFileDownloadAction.is_invoke()


def test_file_filter_is_delegated_with_current_scope(store_factory):
    store, opened_scopes = store_factory
    controller = _controller(controllers.WorkspaceFileController)
    marker = sys_uuid.uuid4()
    controller._pagination_marker = marker

    controller.filter({"stream_uuid": PEER_UUID})

    assert opened_scopes == [(PROJECT_UUID, USER_UUID)]
    operation = store.calls[0]
    assert operation[0:2] == ("filter", "files")
    assert operation[2]["stream_uuid"] == PEER_UUID
    assert operation[2]["uuid"].value == marker
    assert operation[3] == {"uuid": "asc"}


def test_reaction_projection_hides_raw_provider_fields_and_nests_delivery():
    result = sql_store._as_dict(
        {
            "uuid": sys_uuid.uuid4(),
            "provider_uuid": sys_uuid.uuid4(),
            "external_account_uuid": sys_uuid.uuid4(),
            "provider_external_id": "remote-reaction",
            "delivery_status": "failed",
            "delivery_error": "Retry later",
            "delivery_updated_at": "2026-07-16T10:00:00Z",
        },
        "message_reactions",
    )

    assert result["provider"] is None
    assert result["delivery"] == {
        "status": "failed",
        "safe_error": "Retry later",
        "updated_at": "2026-07-16T10:00:00Z",
    }
    assert "provider_uuid" not in result
    assert "external_account_uuid" not in result
    assert "provider_external_id" not in result
    assert "delivery_status" not in result


class _FakeMailRepository:
    def __init__(self):
        self.projection = repository.Projection()

    def rebuild(self):
        return self.projection


def test_file_acl_tracks_canonical_membership_dynamically(monkeypatch):
    monkeypatch.setattr(
        sql_store.SQLProjectedMessengerStore,
        "_latest_projection_event_epoch",
        lambda self: 0,
    )
    mail_repository = _FakeMailRepository()
    service = types.SimpleNamespace(repository=mail_repository)
    store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        USER_UUID,
        service,
    )
    stream_uuid = sys_uuid.uuid4()
    file = types.SimpleNamespace(user_uuid=PEER_UUID, stream_uuid=stream_uuid)

    assert store._can_read_file(file) is False
    binding_uuid = sys_uuid.uuid4()
    mail_repository.projection.bindings[binding_uuid] = {
        "stream_uuid": str(stream_uuid),
        "user_uuid": str(USER_UUID),
    }
    assert store._can_read_file(file) is True
    mail_repository.projection.bindings.pop(binding_uuid)
    assert store._can_read_file(file) is False
    assert (
        store._can_read_file(
            types.SimpleNamespace(user_uuid=USER_UUID, stream_uuid=None)
        )
        is True
    )


def test_public_file_acl_allows_another_authenticated_workspace_user(monkeypatch):
    monkeypatch.setattr(
        sql_store.SQLProjectedMessengerStore,
        "_latest_projection_event_epoch",
        lambda self: 0,
    )
    service = types.SimpleNamespace(repository=_FakeMailRepository())
    store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        USER_UUID,
        service,
    )
    file_uuid = sys_uuid.uuid4()
    file = types.SimpleNamespace(
        uuid=file_uuid,
        user_uuid=PEER_UUID,
        stream_uuid=None,
        storage_type="s3",
    )
    metadata = types.SimpleNamespace(
        uuid=file_uuid,
        owner_uuid=PEER_UUID,
        acl_mode="public",
    )
    monkeypatch.setattr(
        sql_store.file_storage,
        "read_workspace_file_metadata",
        lambda **kwargs: metadata,
    )

    assert store._can_read_file(file) is True


def test_unscoped_file_without_public_sidecar_is_not_visible(monkeypatch):
    monkeypatch.setattr(
        sql_store.SQLProjectedMessengerStore,
        "_latest_projection_event_epoch",
        lambda self: 0,
    )
    service = types.SimpleNamespace(repository=_FakeMailRepository())
    store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        USER_UUID,
        service,
    )
    file = types.SimpleNamespace(
        uuid=sys_uuid.uuid4(),
        user_uuid=PEER_UUID,
        stream_uuid=None,
        storage_type="s3",
    )
    monkeypatch.setattr(
        sql_store.file_storage,
        "read_workspace_file_metadata",
        mock.Mock(side_effect=FileNotFoundError),
    )

    assert store._can_read_file(file) is False


def test_file_create_uses_current_scope_and_storage_metadata(store_factory):
    store, opened_scopes = store_factory
    controller = _controller(controllers.WorkspaceFileController)
    storage_info = controllers.file_storage.WorkspaceFileStorageInfo(
        storage_type="file",
        storage_id="",
        storage_object_id="aa/file",
    )
    with mock.patch.object(
        controllers.file_storage,
        "get_workspace_file_storage_info",
        return_value=storage_info,
    ) as get_storage_info:
        controller.create(
            project_id=sys_uuid.uuid4(),
            user_uuid=sys_uuid.uuid4(),
            stream_uuid=sys_uuid.uuid4(),
            name="example.txt",
            content_type="text/plain",
            size_bytes=12,
            hash="abc",
            storage_type="s3",
            provider_uuid=sys_uuid.uuid4(),
            external_account_uuid=sys_uuid.uuid4(),
        )

    get_storage_info.assert_called_once()
    assert get_storage_info.call_args.kwargs.keys() == {"file_uuid"}
    assert opened_scopes == [(PROJECT_UUID, USER_UUID)]
    values = store.calls[0][2]
    assert values["project_id"] == PROJECT_UUID
    assert values["user_uuid"] == USER_UUID
    assert values["storage_type"] == "file"
    assert values["storage_object_id"] == "aa/file"
    assert "provider_uuid" not in values
    assert "external_account_uuid" not in values


def test_file_json_create_still_requires_stream_uuid(store_factory):
    controller = _controller(controllers.WorkspaceFileController)

    with pytest.raises(Exception):
        controller.create(
            name="example.txt",
            content_type="text/plain",
            size_bytes=12,
            hash="abc",
        )


def test_multipart_upload_builds_metadata_before_store_write(store_factory):
    store, _opened_scopes = store_factory
    controller = _controller(controllers.WorkspaceFileController)
    data = b"example data"
    stream_uuid = sys_uuid.uuid4()
    parts = {
        "file": types.SimpleNamespace(
            file=io.BytesIO(data),
            filename="example.txt",
            type="text/plain",
        ),
        "stream_uuid": types.SimpleNamespace(value=str(stream_uuid)),
        "storage_type": types.SimpleNamespace(value="s3"),
    }
    storage_info = controllers.file_storage.WorkspaceFileStorageInfo(
        storage_type="s3",
        storage_id="workspace-files",
        storage_object_id="key",
    )

    with (
        mock.patch.object(
            controllers.file_storage,
            "save_workspace_file",
            return_value=storage_info,
        ) as save_file,
        mock.patch.object(
            controllers.file_storage,
            "save_workspace_file_metadata",
        ) as save_metadata,
    ):
        controller.create(multipart=True, parts=parts)

    values = store.calls[0][2]
    save_file.assert_called_once_with(
        file_uuid=values["uuid"],
        data=data,
    )
    assert values["stream_uuid"] == stream_uuid
    assert values["hash"] == hashlib.sha256(data).hexdigest()
    assert values["size_bytes"] == len(data)
    assert values["storage_object_id"] == "key"
    metadata = save_metadata.call_args.args[0]
    assert metadata.uuid == values["uuid"]
    assert metadata.project_id == PROJECT_UUID
    assert metadata.stream_uuid == stream_uuid
    assert metadata.owner_uuid == USER_UUID
    assert metadata.sha256 == values["hash"]
    assert metadata.acl_mode == "stream_members"
    save_metadata.assert_called_once_with(metadata, storage_type="s3")


def test_multipart_public_upload_uses_public_acl_without_stream(store_factory):
    store, _opened_scopes = store_factory
    controller = _controller(controllers.WorkspaceFileController)
    data = b"public example data"
    parts = {
        "file": types.SimpleNamespace(
            file=io.BytesIO(data),
            filename="public-example.txt",
            type="text/plain",
        ),
        "acl": types.SimpleNamespace(value='{"mode":"public"}'),
    }
    storage_info = controllers.file_storage.WorkspaceFileStorageInfo(
        storage_type="s3",
        storage_id="workspace-files",
        storage_object_id="public-key",
    )

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
        controller.create(multipart=True, parts=parts)

    values = store.calls[0][2]
    assert values["stream_uuid"] is None
    metadata = save_metadata.call_args.args[0]
    assert metadata.stream_uuid is None
    assert metadata.acl_mode == "public"
    save_metadata.assert_called_once_with(metadata, storage_type="s3")


@pytest.mark.parametrize(
    "parts",
    [
        {},
        {"acl": types.SimpleNamespace(value='{"mode":"stream_members"}')},
        {
            "acl": types.SimpleNamespace(value='{"mode":"public"}'),
            "stream_uuid": types.SimpleNamespace(value=str(sys_uuid.uuid4())),
        },
    ],
)
def test_multipart_upload_rejects_invalid_acl_scope(store_factory, parts):
    controller = _controller(controllers.WorkspaceFileController)
    parts["file"] = types.SimpleNamespace(
        file=io.BytesIO(b"example"),
        filename="example.txt",
        type="text/plain",
    )

    with pytest.raises(Exception):
        controller.create(multipart=True, parts=parts)


def test_file_download_and_delete_use_store_authorization(store_factory):
    store, opened_scopes = store_factory
    controller = _controller(controllers.WorkspaceFileController)
    file_uuid = sys_uuid.uuid4()
    resource = controller.get(file_uuid)

    with (
        mock.patch.object(
            controllers.file_storage,
            "read_workspace_file",
            return_value=b"example data",
        ) as read_file,
        mock.patch.object(
            controllers.file_storage,
            "delete_workspace_file",
        ) as delete_file,
        mock.patch.object(
            controllers.file_storage,
            "delete_workspace_file_metadata",
        ) as delete_metadata,
    ):
        response = controllers.WorkspaceFileController.download._get(
            self=controller,
            resource=resource,
        )
        controller.delete(file_uuid)

    assert response.body == b"example data"
    assert response.content_type == "text/plain"
    assert response.headers["ETag"] == f'"{resource["hash"]}"'
    assert response.headers["Cache-Control"] == "private, no-cache"
    read_file.assert_called_once_with(
        file_uuid=file_uuid,
        storage_type="s3",
        storage_object_id="key",
    )
    assert store.calls == [
        ("get", "files", file_uuid),
        ("get", "files", file_uuid),
        ("delete", "files", file_uuid),
    ]
    assert opened_scopes == [(PROJECT_UUID, USER_UUID), (PROJECT_UUID, USER_UUID)]
    delete_file.assert_called_once_with(
        file_uuid=file_uuid,
        storage_type="s3",
        storage_object_id="key",
    )
    delete_metadata.assert_called_once_with(
        file_uuid=file_uuid,
        storage_type="s3",
    )


def test_file_content_disposition_quotes_and_encodes_name():
    header = controllers.WorkspaceFileController._content_disposition(
        {"name": 'example "file".txt'}
    )

    assert 'filename="example \\"file\\".txt"' in header
    assert "filename*=UTF-8''example%20%22file%22.txt" in header
