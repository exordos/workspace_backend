#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import types
import uuid as sys_uuid
from unittest import mock

from gcl_iam.api import controllers as iam_controllers
from restalchemy.storage import exceptions as storage_exc
from restalchemy.storage.sql import orm

from workspace.messenger_api.api import controllers
from workspace.messenger_api.dm import models


def test_workspace_controller_does_not_use_policy_based_controller():
    assert (
        iam_controllers.PolicyBasedController
        not in controllers.WorkspaceBaseResourceControllerPaginated.mro()
    )


def test_workspace_controller_applies_local_project_and_user_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()

    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceBaseResourceControllerPaginated(request)

    filters = controller._apply_autofilters({})
    values = controller._apply_autovalues({})

    assert filters["project_id"].value == project_id
    assert filters["user_uuid"].value == user_uuid
    assert values["project_id"] == project_id
    assert values["user_uuid"] == user_uuid


def test_topic_controller_autovalues_match_topic_table_shape():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceStreamTopicController(request)

    filters = controller.get_autofilters()
    values = controller.get_autovalues()

    assert filters["project_id"].value == project_id
    assert filters["user_uuid"].value == user_uuid
    assert values == {"project_id": project_id}


def test_stream_controller_hides_private_index():
    resource = controllers.WorkspaceStreamController.__resource__

    assert resource.is_public_field("private_index") is False
    assert resource.is_public_field("name") is True


def test_stream_controller_create_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceStreamController(request)
    returned_stream = object()

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "get_or_create_workspace_user_stream",
        return_value=returned_stream,
    ) as get_or_create:
        result = controller.create(
            project_id=sys_uuid.uuid4(),
            user_uuid=sys_uuid.uuid4(),
            name="Direct",
            description="Private chat",
            source_name="native",
            source={"kind": "native"},
        )

    assert result is returned_stream
    create_kwargs = get_or_create.call_args.kwargs
    assert create_kwargs["project_id"] == project_id
    assert create_kwargs["user_uuid"] == user_uuid


def test_stream_binding_controller_preserves_target_user_uuid():
    project_id = sys_uuid.uuid4()
    actor_uuid = sys_uuid.uuid4()
    target_user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=actor_uuid,
        )
    )
    controller = controllers.WorkspaceStreamBindingController(request)

    with mock.patch.object(models.WorkspaceStreamBinding, "insert") as insert:
        binding = controller.create(
            project_id=sys_uuid.uuid4(),
            stream_uuid=stream_uuid,
            user_uuid=target_user_uuid,
            who_uuid=sys_uuid.uuid4(),
            role=models.WorkspaceStreamRole.MEMBER.value,
        )

    insert.assert_called_once_with()
    assert binding.project_id == project_id
    assert binding.stream_uuid == stream_uuid
    assert binding.user_uuid == target_user_uuid
    assert binding.who_uuid == actor_uuid


def test_stream_binding_controller_returns_existing_binding_on_duplicate():
    project_id = sys_uuid.uuid4()
    actor_uuid = sys_uuid.uuid4()
    target_user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=actor_uuid,
        )
    )
    controller = controllers.WorkspaceStreamBindingController(request)
    existing = models.WorkspaceStreamBinding(
        uuid=sys_uuid.uuid4(),
        project_id=project_id,
        stream_uuid=stream_uuid,
        user_uuid=target_user_uuid,
        who_uuid=actor_uuid,
        role=models.WorkspaceStreamRole.MEMBER.value,
    )
    conflict = storage_exc.ConflictRecords(
        model="binding",
        msg="duplicate key value violates unique constraint",
    )

    with mock.patch.object(
        models.WorkspaceStreamBinding,
        "insert",
        side_effect=conflict,
    ):
        with mock.patch.object(
            orm.ObjectCollection,
            "get_one_or_none",
            return_value=existing,
        ) as get_one_or_none:
            binding = controller.create(
                stream_uuid=stream_uuid,
                user_uuid=target_user_uuid,
                role=models.WorkspaceStreamRole.MEMBER.value,
            )

    assert binding is existing
    filters = get_one_or_none.call_args.kwargs["filters"]
    assert filters["project_id"].value == project_id
    assert filters["stream_uuid"].value == stream_uuid
    assert filters["user_uuid"].value == target_user_uuid
