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

import hashlib
import io
import types
import uuid as sys_uuid
from unittest import mock

from gcl_iam.api import controllers as iam_controllers
from restalchemy.api import routes as ra_routes

from workspace.messenger_api.api import controllers
from workspace.messenger_api.api import routes
from workspace.messenger_api.dm import models


def test_workspace_controller_does_not_use_policy_based_controller():
    assert (
        iam_controllers.PolicyBasedController
        not in controllers.WorkspaceBaseResourceControllerPaginated.mro()
    )


def test_workspace_user_get_syncs_current_iam_identity():
    user_uuid = sys_uuid.uuid4()
    iam_user = types.SimpleNamespace(
        name="cassi",
        first_name="Cassandra",
        last_name="Volkova",
        email="cassi@exordos.com",
    )
    iam_context = types.SimpleNamespace(
        get_introspection_info=mock.Mock(
            return_value=types.SimpleNamespace(user_info=iam_user),
        ),
    )
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            user_uuid=user_uuid,
            iam_context=iam_context,
        ),
    )
    controller = controllers.WorkspaceUserController(request)
    returned_user = object()

    with (
        mock.patch.object(
            models.WorkspaceUser,
            "sync_iam_identity",
        ) as sync_identity,
        mock.patch.object(
            controllers.ra_controllers.BaseResourceControllerPaginated,
            "get",
            return_value=returned_user,
        ) as parent_get,
    ):
        result = controller.get(user_uuid)

    assert result is returned_user
    sync_identity.assert_called_once_with(
        user_uuid=user_uuid,
        username="cassi",
        first_name="Cassandra",
        last_name="Volkova",
        email="cassi@exordos.com",
    )
    parent_get.assert_called_once_with(user_uuid)


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


def test_topic_controller_create_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceStreamTopicController(request)
    returned_topic = object()

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "create_workspace_user_stream_topic",
        return_value=returned_topic,
    ) as create_topic:
        result = controller.create(
            project_id=sys_uuid.uuid4(),
            name="planning",
            stream_uuid=stream_uuid,
        )

    assert result is returned_topic
    create_topic.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        values={
            "name": "planning",
            "stream_uuid": stream_uuid,
        },
    )


def test_topic_controller_update_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceStreamTopicController(request)
    returned_topic = object()

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "update_workspace_user_stream_topic",
        return_value=returned_topic,
    ) as update_topic:
        result = controller.update(topic_uuid, name="retros")

    assert result is returned_topic
    update_topic.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        topic_uuid=topic_uuid,
        values={"name": "retros"},
    )


def test_topic_controller_delete_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceStreamTopicController(request)

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "delete_workspace_user_stream_topic",
        return_value=None,
    ) as delete_topic:
        result = controller.delete(topic_uuid)

    assert result is None
    delete_topic.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        topic_uuid=topic_uuid,
    )


def test_topic_controller_toggle_done_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceStreamTopicController(request)
    resource = types.SimpleNamespace(uuid=topic_uuid)
    returned_topic = object()

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "toggle_workspace_user_stream_topic_done",
        return_value=returned_topic,
    ) as toggle_topic:
        result = controllers.WorkspaceStreamTopicController.toggle_done._post(
            self=controller,
            resource=resource,
        )

    assert result is returned_topic
    toggle_topic.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        topic_uuid=topic_uuid,
    )


def test_topic_controller_set_default_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceStreamTopicController(request)
    resource = types.SimpleNamespace(uuid=topic_uuid)
    returned_topic = object()

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "set_workspace_user_stream_topic_default",
        return_value=returned_topic,
    ) as set_default:
        result = controllers.WorkspaceStreamTopicController.set_default._post(
            self=controller,
            resource=resource,
        )

    assert result is returned_topic
    set_default.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        topic_uuid=topic_uuid,
    )


def test_topic_controller_notifications_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceStreamTopicController(request)
    resource = types.SimpleNamespace(uuid=topic_uuid)
    returned_topic = object()

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "update_workspace_user_stream_topic_notifications",
        return_value=returned_topic,
    ) as update_notifications:
        result = controllers.WorkspaceStreamTopicController.notifications._post(
            self=controller,
            resource=resource,
            notification_mode="follow",
        )

    assert result is returned_topic
    update_notifications.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        topic_uuid=topic_uuid,
        notification_mode="follow",
    )


def test_topic_controller_read_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceStreamTopicController(request)
    resource = types.SimpleNamespace(uuid=topic_uuid)
    returned_topic = object()

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "read_workspace_user_stream_topic_messages",
        return_value=returned_topic,
    ) as read_topic:
        result = controllers.WorkspaceStreamTopicController.read._post(
            self=controller,
            resource=resource,
        )

    assert result is returned_topic
    read_topic.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        topic_uuid=topic_uuid,
    )


def test_message_controller_update_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceMessageController(request)
    returned_message = object()
    payload = {"kind": "markdown", "content": "edited"}

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "update_workspace_user_message",
        return_value=returned_message,
    ) as update_message:
        result = controller.update(message_uuid, payload=payload)

    assert result is returned_message
    update_message.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        message_uuid=message_uuid,
        values={"payload": payload},
    )


def test_message_controller_delete_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceMessageController(request)

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "delete_workspace_user_message",
        return_value=None,
    ) as delete_message:
        result = controller.delete(message_uuid)

    assert result is None
    delete_message.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        message_uuid=message_uuid,
    )


def test_message_controller_read_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceMessageController(request)
    resource = types.SimpleNamespace(uuid=message_uuid)
    returned_message = object()

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "read_workspace_user_message",
        return_value=returned_message,
    ) as read_message:
        result = controllers.WorkspaceMessageController.read._post(
            self=controller,
            resource=resource,
        )

    assert result is returned_message
    read_message.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        message_uuid=message_uuid,
    )


def test_message_controller_read_up_to_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceMessageController(request)
    resource = types.SimpleNamespace(uuid=message_uuid)
    returned_message = object()

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "read_workspace_user_topic_messages_to_message",
        return_value=returned_message,
    ) as read_messages:
        result = controllers.WorkspaceMessageController.read_up_to._post(
            self=controller,
            resource=resource,
        )

    assert result is returned_message
    read_messages.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        message_uuid=message_uuid,
    )



def test_message_reaction_controller_reads_all_visible_message_reactions():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    first_message_uuid = sys_uuid.uuid4()
    second_message_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceMessageReactionController(request)

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "get_workspace_user_message_uuids",
        return_value=[first_message_uuid, second_message_uuid],
    ) as get_message_uuids:
        filters = controller.get_autofilters()

    assert filters["project_id"].value == project_id
    assert filters["message_uuid"].value == [
        first_message_uuid,
        second_message_uuid,
    ]
    assert "user_uuid" not in filters
    get_message_uuids.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
    )


def test_message_reaction_controller_create_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    reaction_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceMessageReactionController(request)
    returned_reaction = object()

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "create_workspace_message_reaction",
        return_value=returned_reaction,
    ) as create_reaction:
        result = controller.create(
            uuid=reaction_uuid,
            project_id=sys_uuid.uuid4(),
            user_uuid=sys_uuid.uuid4(),
            message_uuid=message_uuid,
            emoji_name="thumbs_up",
        )

    assert result is returned_reaction
    create_reaction.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        uuid=reaction_uuid,
        message_uuid=message_uuid,
        emoji_name="thumbs_up",
    )


def test_message_reaction_controller_update_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    reaction_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceMessageReactionController(request)
    returned_reaction = object()

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "update_workspace_message_reaction",
        return_value=returned_reaction,
    ) as update_reaction:
        result = controller.update(
            reaction_uuid,
            emoji_name="heart",
        )

    assert result is returned_reaction
    update_reaction.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        reaction_uuid=reaction_uuid,
        values={"emoji_name": "heart"},
    )


def test_message_reaction_controller_delete_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    reaction_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceMessageReactionController(request)

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "delete_workspace_message_reaction",
        return_value=None,
    ) as delete_reaction:
        result = controller.delete(reaction_uuid)

    assert result is None
    delete_reaction.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        reaction_uuid=reaction_uuid,
    )


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


def test_stream_controller_update_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
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
        "update_workspace_user_stream",
        return_value=returned_stream,
    ) as update_stream:
        result = controller.update(
            uuid=stream_uuid,
            name="Core Team",
            description="Core team chat",
            invite_only=True,
        )

    assert result is returned_stream
    update_stream.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=stream_uuid,
        values={
            "name": "Core Team",
            "description": "Core team chat",
            "invite_only": True,
        },
    )


def test_stream_controller_delete_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
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
        "delete_workspace_user_stream",
        return_value=returned_stream,
    ) as delete_stream:
        result = controller.delete(uuid=stream_uuid)

    assert result is returned_stream
    delete_stream.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=stream_uuid,
    )


def test_stream_route_allows_delete():
    assert ra_routes.DELETE in routes.WorkspaceStreamRoute.__allow_methods__


def test_stream_route_allows_update():
    assert ra_routes.UPDATE in routes.WorkspaceStreamRoute.__allow_methods__


def test_message_route_allows_update_and_delete():
    assert ra_routes.UPDATE in routes.WorkspaceMessageRoute.__allow_methods__
    assert ra_routes.DELETE in routes.WorkspaceMessageRoute.__allow_methods__


def test_message_reaction_route_allows_crud():
    allowed_methods = routes.WorkspaceMessageReactionRoute.__allow_methods__

    assert ra_routes.CREATE in allowed_methods
    assert ra_routes.FILTER in allowed_methods
    assert ra_routes.GET in allowed_methods
    assert ra_routes.UPDATE in allowed_methods
    assert ra_routes.DELETE in allowed_methods


def test_stream_controller_archive_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceStreamController(request)
    resource = types.SimpleNamespace(uuid=stream_uuid)
    returned_stream = object()

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "update_workspace_user_stream",
        return_value=returned_stream,
    ) as update_stream:
        result = controllers.WorkspaceStreamController.archive._post(
            self=controller,
            resource=resource,
        )

    assert result is returned_stream
    update_stream.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=stream_uuid,
        values={"is_archived": True},
    )


def test_stream_controller_unarchive_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceStreamController(request)
    resource = types.SimpleNamespace(uuid=stream_uuid)
    returned_stream = object()

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "update_workspace_user_stream",
        return_value=returned_stream,
    ) as update_stream:
        result = controllers.WorkspaceStreamController.unarchive._post(
            self=controller,
            resource=resource,
        )

    assert result is returned_stream
    update_stream.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=stream_uuid,
        values={"is_archived": False},
    )


def test_stream_controller_notifications_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceStreamController(request)
    resource = types.SimpleNamespace(uuid=stream_uuid)
    returned_stream = object()

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "update_workspace_user_stream_notifications",
        return_value=returned_stream,
    ) as update_notifications:
        result = controllers.WorkspaceStreamController.notifications._post(
            self=controller,
            resource=resource,
            notification_mode="mentions_only",
        )

    assert result is returned_stream
    update_notifications.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=stream_uuid,
        notification_mode="mentions_only",
    )


def test_stream_controller_read_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceStreamController(request)
    resource = types.SimpleNamespace(uuid=stream_uuid)
    returned_stream = object()

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "read_workspace_user_stream_messages",
        return_value=returned_stream,
    ) as read_stream:
        result = controllers.WorkspaceStreamController.read._post(
            self=controller,
            resource=resource,
        )

    assert result is returned_stream
    read_stream.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        stream_uuid=stream_uuid,
    )


def test_stream_controller_add_users_uses_context_and_stream_resource():
    project_id = sys_uuid.uuid4()
    actor_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=actor_uuid,
        )
    )
    controller = controllers.WorkspaceStreamController(request)
    resource = types.SimpleNamespace(
        project_id=project_id,
        uuid=stream_uuid,
    )
    returned_bindings = [object()]
    payload = {
        models.WorkspaceStreamRole.MEMBER.value: [sys_uuid.uuid4()],
        models.WorkspaceStreamRole.OWNER.value: [sys_uuid.uuid4()],
    }

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "get_or_create_workspace_stream_bindings",
        return_value=returned_bindings,
    ) as get_or_create:
        result = controllers.WorkspaceStreamController.add_users._post(
            self=controller,
            resource=resource,
            **payload,
        )

    assert result is returned_bindings
    get_or_create.assert_called_once_with(
        project_id=project_id,
        stream_uuid=stream_uuid,
        who_uuid=actor_uuid,
        role_user_uuids=payload,
    )


def test_stream_binding_controller_delete_uses_context_scope():
    project_id = sys_uuid.uuid4()
    actor_uuid = sys_uuid.uuid4()
    binding_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=actor_uuid,
        )
    )
    controller = controllers.WorkspaceStreamBindingController(request)

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "delete_workspace_stream_binding",
        return_value=None,
    ) as delete_binding:
        result = controller.delete(binding_uuid)

    assert result is None
    delete_binding.assert_called_once_with(
        project_id=project_id,
        binding_uuid=binding_uuid,
    )


def test_stream_bindings_action_uses_stream_controller_resource():
    assert (
        routes.WorkspaceStreamBindingsAction.__controller__
        is controllers.WorkspaceStreamBindingsActionController
    )


def test_stream_archive_actions_use_stream_controller_resource():
    assert (
        routes.WorkspaceStreamArchiveAction.__controller__
        is controllers.WorkspaceStreamController
    )
    assert (
        routes.WorkspaceStreamUnarchiveAction.__controller__
        is controllers.WorkspaceStreamController
    )
    assert (
        routes.WorkspaceStreamNotificationsAction.__controller__
        is controllers.WorkspaceStreamController
    )
    assert (
        routes.WorkspaceStreamReadAction.__controller__
        is controllers.WorkspaceStreamController
    )


def test_topic_notifications_action_uses_topic_controller_resource():
    assert (
        routes.WorkspaceStreamTopicNotificationsAction.__controller__
        is controllers.WorkspaceStreamTopicController
    )
    assert (
        routes.WorkspaceStreamTopicReadAction.__controller__
        is controllers.WorkspaceStreamTopicController
    )


def test_message_read_action_uses_message_controller_resource():
    assert (
        routes.WorkspaceMessageReadAction.__controller__
        is controllers.WorkspaceMessageController
    )
    assert (
        routes.WorkspaceMessageReadUpToAction.__controller__
        is controllers.WorkspaceMessageController
    )


def test_stream_binding_route_does_not_allow_create():
    assert ra_routes.CREATE not in routes.WorkspaceStreamBindingRoute.__allow_methods__


def test_file_route_registered_and_allows_crud():
    assert routes.ApiEndpointRoute.files is routes.WorkspaceFileRoute
    assert ra_routes.CREATE in routes.WorkspaceFileRoute.__allow_methods__
    assert ra_routes.FILTER in routes.WorkspaceFileRoute.__allow_methods__
    assert ra_routes.GET in routes.WorkspaceFileRoute.__allow_methods__
    assert ra_routes.UPDATE in routes.WorkspaceFileRoute.__allow_methods__
    assert ra_routes.DELETE in routes.WorkspaceFileRoute.__allow_methods__
    assert routes.WorkspaceFileDownloadAction.__controller__ is controllers.WorkspaceFileController
    assert not routes.WorkspaceFileDownloadAction.is_invoke()


def test_file_controller_create_uses_context_scope():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceFileController(request)
    returned_file = object()

    storage_info = controllers.file_storage.WorkspaceFileStorageInfo(
        storage_type="file",
        storage_id="",
        storage_object_id="aa/file",
    )

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "create_workspace_file",
        return_value=returned_file,
    ) as create_file, mock.patch.object(
        controllers.file_storage,
        "get_workspace_file_storage_info",
        return_value=storage_info,
    ) as get_storage_info:
        result = controller.create(
            project_id=sys_uuid.uuid4(),
            user_uuid=sys_uuid.uuid4(),
            stream_uuid=stream_uuid,
            name="example.txt",
            description="Example",
            content_type="text/plain",
            size_bytes=12,
            hash="abc",
        )

    assert result is returned_file
    create_kwargs = create_file.call_args.kwargs
    assert create_kwargs["project_id"] == project_id
    assert create_kwargs["user_uuid"] == user_uuid
    assert create_kwargs["stream_uuid"] == stream_uuid
    assert create_kwargs["name"] == "example.txt"
    assert create_kwargs["hash"] == "abc"
    assert create_kwargs["storage_type"] == "file"
    assert create_kwargs["storage_id"] == ""
    assert create_kwargs["storage_object_id"] == "aa/file"
    get_storage_info.assert_called_once_with(
        file_uuid=create_kwargs["uuid"],
        storage_type=None,
    )


def test_file_controller_autofilters_use_accesses():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    file_uuids = [sys_uuid.uuid4(), sys_uuid.uuid4()]
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceFileController(request)

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "get_workspace_user_file_uuids",
        return_value=file_uuids,
    ) as get_file_uuids:
        filters = controller.get_autofilters()

    get_file_uuids.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
    )
    assert filters["project_id"].value == project_id
    assert filters["uuid"].value == file_uuids


def test_file_controller_create_from_multipart_builds_file_metadata():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceFileController(request)
    data = b"example data"
    file_part = types.SimpleNamespace(
        file=io.BytesIO(data),
        filename="example.txt",
        type="text/plain",
    )
    parts = {
        "file": file_part,
        "stream_uuid": types.SimpleNamespace(value=str(stream_uuid)),
        "storage_type": types.SimpleNamespace(value="s3"),
    }
    returned_file = object()
    storage_info = controllers.file_storage.WorkspaceFileStorageInfo(
        storage_type="s3",
        storage_id="workspace-files",
        storage_object_id="key",
    )

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "create_workspace_file",
        return_value=returned_file,
    ) as create_file, mock.patch.object(
        controllers.file_storage,
        "save_workspace_file",
        return_value=storage_info,
    ) as save_file:
        result = controller.create(multipart=True, parts=parts)

    assert result is returned_file
    create_kwargs = create_file.call_args.kwargs
    assert create_kwargs["project_id"] == project_id
    assert create_kwargs["user_uuid"] == user_uuid
    assert create_kwargs["stream_uuid"] == stream_uuid
    assert create_kwargs["name"] == "example.txt"
    save_file.assert_called_once_with(
        file_uuid=create_kwargs["uuid"],
        data=data,
        storage_type="s3",
    )
    assert create_kwargs["content_type"] == "text/plain"
    assert create_kwargs["size_bytes"] == len(data)
    assert create_kwargs["hash"] == hashlib.sha256(data).hexdigest()
    assert create_kwargs["storage_type"] == "s3"
    assert create_kwargs["storage_id"] == "workspace-files"
    assert create_kwargs["storage_object_id"] == "key"


def test_file_controller_download_returns_file_response():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    file_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceFileController(request)
    resource = types.SimpleNamespace(
        uuid=file_uuid,
        name='example "file".txt',
        content_type="text/plain",
        storage_type="s3",
        storage_object_id="key",
    )
    data = b"example data"

    with mock.patch.object(
        controllers.file_storage,
        "read_workspace_file",
        return_value=data,
    ) as read_file:
        response = controller._download_file_response(resource)

    read_file.assert_called_once_with(
        file_uuid=file_uuid,
        storage_type="s3",
        storage_object_id="key",
    )
    assert response.status_int == 200
    assert response.body == data
    assert response.content_type == "text/plain"
    content_disposition = response.headers["Content-Disposition"]
    assert 'filename="example \\"file\\".txt"' in content_disposition



def test_file_controller_delete_removes_backend_object():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    file_uuid = sys_uuid.uuid4()
    request = types.SimpleNamespace(
        context=types.SimpleNamespace(
            project_id=project_id,
            user_uuid=user_uuid,
        )
    )
    controller = controllers.WorkspaceFileController(request)
    file = types.SimpleNamespace(
        storage_type="s3",
        storage_object_id="key",
    )

    with mock.patch.object(
        controllers.messenger_dm_helpers,
        "get_workspace_owned_file",
        return_value=file,
    ) as get_file, mock.patch.object(
        controllers.messenger_dm_helpers,
        "delete_workspace_file",
    ) as delete_file, mock.patch.object(
        controllers.file_storage,
        "delete_workspace_file",
    ) as delete_stored_file:
        result = controller.delete(file_uuid)

    assert result is delete_file.return_value
    get_file.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        file_uuid=file_uuid,
    )
    delete_file.assert_called_once_with(
        project_id=project_id,
        user_uuid=user_uuid,
        file_uuid=file_uuid,
    )
    delete_stored_file.assert_called_once_with(
        file_uuid=file_uuid,
        storage_type="s3",
        storage_object_id="key",
    )
