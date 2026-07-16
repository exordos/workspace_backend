# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import contextlib
import inspect
import types
import uuid as sys_uuid

import pytest

from workspace.messenger_api.api import controllers
from workspace.messenger_api.api import routes
from workspace.messenger_api.api import store as api_store
from workspace.messenger_mail import repository


PROJECT_UUID = sys_uuid.UUID("10000000-0000-0000-0000-000000000001")
USER_UUID = sys_uuid.UUID("20000000-0000-0000-0000-000000000002")
PEER_UUID = sys_uuid.UUID("30000000-0000-0000-0000-000000000003")
MESSAGE_UUID = sys_uuid.UUID("40000000-0000-0000-0000-000000000004")


class FakeStore:
    def __init__(self):
        self.calls = []

    def filter_resources(self, resource, filters, order_by=None):
        self.calls.append(("filter", resource, filters, order_by))
        return []

    def sync_iam_identity(self, values):
        self.calls.append(("sync_iam_identity", values))
        return values.copy()

    def get_resource(self, resource, resource_uuid):
        self.calls.append(("get", resource, resource_uuid))
        return {"uuid": resource_uuid}

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
        self.calls.append(("events_after", filters, order_by))
        return [{"epoch_version": 2}]

    def current_epoch(self):
        self.calls.append(("current_epoch",))
        return 7

    def event_cursor(self):
        self.calls.append(("event_cursor",))
        return {
            "epoch_generation": "91",
            "current_epoch_version": 7,
            "minimum_epoch_version": 2,
        }


@pytest.fixture
def fake_store():
    value = FakeStore()
    api_store.configure_store_factory(
        lambda project_uuid, user_uuid: contextlib.nullcontext(value)
    )
    try:
        yield value
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


def test_message_mutations_use_dedicated_canonical_mail_operations(fake_store):
    controller = _controller(controllers.WorkspaceMessageController)

    created = controller.create(
        uuid=MESSAGE_UUID,
        stream_uuid=sys_uuid.uuid4(),
        topic_uuid=sys_uuid.uuid4(),
        payload={"kind": "markdown", "content": "hello"},
    )
    updated = controller.update(
        MESSAGE_UUID,
        payload={"kind": "markdown", "content": "edited"},
    )
    deleted = controller.delete(MESSAGE_UUID)

    assert created["source_name"] == "native"
    assert created["source"] == {"kind": "native"}
    assert created["provider"] is None
    assert created["delivery"] is None
    assert updated["uuid"] == MESSAGE_UUID
    assert deleted == {"uuid": MESSAGE_UUID}
    assert [call[0] for call in fake_store.calls] == [
        "create_message",
        "update_message",
        "delete_message",
    ]


def test_direct_chat_is_an_ordinary_stream_with_deterministic_pair_uuid(fake_store):
    controller = _controller(controllers.WorkspaceStreamController)

    result = controller.create(
        name="Direct chat",
        direct_user_uuid=PEER_UUID,
        invite_only=False,
    )

    expected_uuid = repository.deterministic_dm_uuid(
        PROJECT_UUID,
        USER_UUID,
        PEER_UUID,
    )
    assert result["uuid"] == expected_uuid
    assert result["private"] is True
    assert result["direct_user_uuid"] == PEER_UUID
    assert fake_store.calls[0][0:2] == ("create", "streams")

    with pytest.raises(Exception):
        controller.create(name="Self", direct_user_uuid=USER_UUID)


def test_events_and_epoch_are_read_through_store_boundary(fake_store):
    events = _controller(controllers.WorkspaceEventController).filter(
        {"epoch_version": 1},
        {"epoch_version": "asc"},
    )
    epoch = _controller(controllers.WorkspaceEpochController).filter({})

    assert events == [{"epoch_version": 2}]
    assert epoch == {
        "epoch_version": 7,
        "epoch_generation": "91",
        "current_epoch_version": 7,
        "minimum_epoch_version": 2,
    }
    assert fake_store.calls == [
        (
            "events_after",
            {"epoch_version": 1},
            {"epoch_version": "asc"},
        ),
        ("event_cursor",),
    ]


def test_generic_pagination_only_marks_a_proven_next_page():
    controller = _controller(controllers.FolderController)
    controller._pagination_limit = 2
    first = {"uuid": sys_uuid.uuid4()}
    second = {"uuid": sys_uuid.uuid4()}
    third = {"uuid": sys_uuid.uuid4()}

    assert controller._paginate_result([first, second]) == [first, second]
    assert controller._pagination_has_more is False
    assert controller._paginate_result([first, second, third]) == [first, second]
    assert controller._pagination_has_more is True


def test_reaction_mutations_reject_internal_provider_projection_fields(fake_store):
    controller = _controller(controllers.WorkspaceMessageReactionController)

    with pytest.raises(Exception):
        controller.create(
            message_uuid=MESSAGE_UUID,
            emoji_name="eyes",
            provider_uuid=sys_uuid.uuid4(),
        )
    with pytest.raises(Exception):
        controller.update(
            sys_uuid.uuid4(),
            delivery_status="pending",
        )

    assert fake_store.calls == []


def test_external_accounts_are_not_part_of_messenger_routes():
    assert not hasattr(routes.ApiEndpointRoute, "external_accounts")
    assert not hasattr(routes, "ExternalAccountRoute")


def test_controllers_do_not_access_sql_objects_or_deleted_provider_api():
    source = inspect.getsource(controllers)

    assert ".objects" not in source
    assert "provider_api" not in source
    assert "messenger_dm_helpers" not in source
