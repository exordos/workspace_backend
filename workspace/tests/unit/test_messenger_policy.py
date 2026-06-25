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

from gcl_iam.api import controllers as iam_controllers

from workspace.messenger_api.api import controllers


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
