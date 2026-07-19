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

import typing

from restalchemy.api import constants
from restalchemy.api import routes

from workspace.messenger_api.api import controllers


class FolderItemPinAction(routes.Action):
    __controller__ = controllers.FolderItemController


class FolderItemUnpinAction(routes.Action):
    __controller__ = controllers.FolderItemController


class WorkspaceStreamBindingsAction(routes.Action):
    __controller__ = controllers.WorkspaceStreamBindingController


class WorkspaceMessageReadAction(routes.Action):
    __controller__ = controllers.WorkspaceMessageController


class WorkspaceMessageReadUpToAction(routes.Action):
    __controller__ = controllers.WorkspaceMessageController


class ExternalAccountReconnectAction(routes.Action):
    __controller__ = controllers.ExternalAccountController


class ExternalAccountDisconnectAction(routes.Action):
    __controller__ = controllers.ExternalAccountController


class ExternalChatSelectAction(routes.Action):
    __controller__ = controllers.ExternalChatController


class ExternalChatDeselectAction(routes.Action):
    __controller__ = controllers.ExternalChatController


class ExternalChatMoveAction(routes.Action):
    __controller__ = controllers.ExternalChatController


class ExternalOperationRetryAction(routes.Action):
    __controller__ = controllers.ExternalOperationController


class ExternalOperationPreflightAction(routes.Action):
    __controller__ = controllers.ExternalOperationController


ExternalOperationPreflightActionRoute = routes.action(
    ExternalOperationPreflightAction,
    invoke=True,
)


class ExternalBridgeInstanceSuspendAction(routes.Action):
    __controller__ = controllers.ExternalBridgeInstanceController


class ExternalBridgeInstanceResumeAction(routes.Action):
    __controller__ = controllers.ExternalBridgeInstanceController


class ExternalBridgeInstanceRevokeAction(routes.Action):
    __controller__ = controllers.ExternalBridgeInstanceController


class ExternalProviderPolicySuspendAction(routes.Action):
    __controller__ = controllers.ExternalProviderPolicyController


class ExternalProviderPolicyResumeAction(routes.Action):
    __controller__ = controllers.ExternalProviderPolicyController


class WorkspaceStreamArchiveAction(routes.Action):
    __controller__ = controllers.WorkspaceStreamController


class WorkspaceStreamUnarchiveAction(routes.Action):
    __controller__ = controllers.WorkspaceStreamController


class WorkspaceStreamNotificationsAction(routes.Action):
    __controller__ = controllers.WorkspaceStreamController


class WorkspaceStreamReadAction(routes.Action):
    __controller__ = controllers.WorkspaceStreamController


class WorkspaceFileDownloadAction(routes.Action):
    __controller__ = controllers.WorkspaceFileController


class FolderItemRoute(routes.Route):
    __controller__ = controllers.FolderItemController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.DELETE,
    ]

    pin = routes.action(FolderItemPinAction, invoke=True)
    unpin = routes.action(FolderItemUnpinAction, invoke=True)


class WorkspaceFileRoute(routes.Route):
    __controller__ = controllers.WorkspaceFileController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.UPDATE,
        routes.DELETE,
    ]

    download = routes.action(WorkspaceFileDownloadAction)


class FolderRoute(routes.Route):
    __controller__ = controllers.FolderController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.UPDATE,
        routes.DELETE,
    ]


class WorkspaceStreamRoute(routes.Route):
    __controller__ = controllers.WorkspaceStreamController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.UPDATE,
        routes.DELETE,
    ]

    add_users = routes.action(WorkspaceStreamBindingsAction, invoke=True)
    archive = routes.action(WorkspaceStreamArchiveAction, invoke=True)
    unarchive = routes.action(WorkspaceStreamUnarchiveAction, invoke=True)
    notifications = routes.action(
        WorkspaceStreamNotificationsAction,
        invoke=True,
    )
    read = routes.action(WorkspaceStreamReadAction, invoke=True)

    @classmethod
    def get_actions_by_names(
        cls,
        names: typing.Iterable[str],
    ) -> list[typing.Any]:
        return [
            getattr(cls.get_action(name).get_controller_class(), name) for name in names
        ]


class WorkspaceStreamBindingRoute(routes.Route):
    __controller__ = controllers.WorkspaceStreamBindingController
    __allow_methods__ = [
        routes.FILTER,
        routes.GET,
        routes.UPDATE,
        routes.DELETE,
    ]


class WorkspaceMessageRoute(routes.Route):
    __controller__ = controllers.WorkspaceMessageController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.UPDATE,
        routes.DELETE,
    ]

    read = routes.action(WorkspaceMessageReadAction, invoke=True)
    read_up_to = routes.action(WorkspaceMessageReadUpToAction, invoke=True)


class WorkspaceDraftRoute(routes.Route):
    __controller__ = controllers.WorkspaceDraftController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.UPDATE,
        routes.DELETE,
    ]


class ExternalAccountRoute(routes.Route):
    __controller__ = controllers.ExternalAccountController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.UPDATE,
        routes.DELETE,
    ]

    reconnect = routes.action(ExternalAccountReconnectAction, invoke=True)
    disconnect = routes.action(ExternalAccountDisconnectAction, invoke=True)


class ExternalChatRoute(routes.Route):
    __controller__ = controllers.ExternalChatController
    __allow_methods__ = [routes.FILTER, routes.GET]

    select = routes.action(ExternalChatSelectAction, invoke=True)
    deselect = routes.action(ExternalChatDeselectAction, invoke=True)
    move = routes.action(ExternalChatMoveAction, invoke=True)


class ExternalOperationActionsRoute(routes.Route):
    __controller__ = controllers.ExternalOperationController
    __allow_methods__: list[str] = []

    def do(
        self,
        parent_resource: typing.Any = None,
        **kwargs: typing.Any,
    ) -> typing.Any:
        del parent_resource, kwargs
        return ExternalOperationPreflightActionRoute(self._req).do(resource=None)


class ExternalOperationRoute(routes.Route):
    __controller__ = controllers.ExternalOperationController
    __allow_methods__ = [routes.FILTER, routes.GET, routes.DELETE]

    retry = routes.action(ExternalOperationRetryAction, invoke=True)
    actions = routes.route(ExternalOperationActionsRoute)


class ExternalBridgeInstanceRoute(routes.Route):
    __controller__ = controllers.ExternalBridgeInstanceController
    __allow_methods__ = [routes.FILTER, routes.GET]

    suspend = routes.action(ExternalBridgeInstanceSuspendAction, invoke=True)
    resume = routes.action(ExternalBridgeInstanceResumeAction, invoke=True)
    revoke = routes.action(ExternalBridgeInstanceRevokeAction, invoke=True)


class ExternalProviderPolicyRoute(routes.Route):
    __controller__ = controllers.ExternalProviderPolicyController
    __allow_methods__ = [routes.GET, routes.UPDATE]

    suspend = routes.action(ExternalProviderPolicySuspendAction, invoke=True)
    resume = routes.action(ExternalProviderPolicyResumeAction, invoke=True)


class ExternalProviderHealthRoute(routes.Route):
    __controller__ = controllers.ExternalProviderHealthController
    __allow_methods__ = [routes.GET]


class WorkspaceMessageReactionRoute(routes.Route):
    __controller__ = controllers.WorkspaceMessageReactionController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.UPDATE,
        routes.DELETE,
    ]


class WorkspaceEventRoute(routes.Route):
    __controller__ = controllers.WorkspaceEventController
    __allow_methods__ = [
        routes.FILTER,
    ]


class WorkspaceEpochRoute(routes.Route):
    __controller__ = controllers.WorkspaceEpochController
    __allow_methods__ = [
        routes.FILTER,
    ]


class WorkspaceStreamTopicToggleDoneRoute(routes.Route):
    __controller__ = controllers.WorkspaceStreamTopicController
    __allow_methods__ = [
        routes.CREATE,
    ]

    def do(
        self,
        parent_resource: typing.Any = None,
        **kwargs: typing.Any,
    ) -> typing.Any:
        controller = self.get_controller(request=self._req)
        return controller.toggle_done.do_post(
            controller=controller,
            resource=parent_resource,
        )


class WorkspaceStreamTopicNotificationsAction(routes.Action):
    __controller__ = controllers.WorkspaceStreamTopicController


class WorkspaceStreamTopicSetDefaultAction(routes.Action):
    __controller__ = controllers.WorkspaceStreamTopicController


class WorkspaceStreamTopicReadAction(routes.Action):
    __controller__ = controllers.WorkspaceStreamTopicController


class WorkspaceUserPresenceAction(routes.Action):
    __controller__ = controllers.WorkspaceUserController


class WorkspaceUserAvatarUploadAction(routes.Action):
    __controller__ = controllers.WorkspaceUserController

    def do(
        self,
        resource: typing.Any,
        **kwargs: typing.Any,
    ) -> typing.Any:
        if constants.CONTENT_TYPE_MULTIPART in self._req.content_type:
            controller = self.get_controller(self._req)
            packer = controller.get_packer(self._req.content_type)
            packer._rt = None
            kwargs.update(packer.unpack(value=self._req.body))
        return super().do(resource, **kwargs)


class WorkspaceUserAvatarResetAction(routes.Action):
    __controller__ = controllers.WorkspaceUserController


class WorkspaceStreamTopicRoute(routes.Route):
    __controller__ = controllers.WorkspaceStreamTopicController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.UPDATE,
        routes.DELETE,
    ]

    toggle_done = routes.route(
        WorkspaceStreamTopicToggleDoneRoute,
        resource_route=True,
    )
    notifications = routes.action(
        WorkspaceStreamTopicNotificationsAction,
        invoke=True,
    )
    set_default = routes.action(
        WorkspaceStreamTopicSetDefaultAction,
        invoke=True,
    )
    read = routes.action(WorkspaceStreamTopicReadAction, invoke=True)


class WorkspaceUserRoute(routes.Route):
    __controller__ = controllers.WorkspaceUserController
    __allow_methods__ = [
        routes.FILTER,
        routes.GET,
    ]

    presence = routes.action(WorkspaceUserPresenceAction, invoke=True)
    avatar_upload = routes.action(
        WorkspaceUserAvatarUploadAction,
        invoke=True,
    )
    avatar_reset = routes.action(
        WorkspaceUserAvatarResetAction,
        invoke=True,
    )


class MeRoute(routes.Route):
    """Handler for the current IAM user endpoint."""

    __controller__ = controllers.MeController
    __allow_methods__ = [routes.FILTER]


class ApiEndpointRoute(routes.Route):
    """Handler for /v1/ endpoint."""

    __controller__ = controllers.ApiEndpointController
    __allow_methods__ = [routes.FILTER]

    folders = routes.route(FolderRoute)
    folder_items = routes.route(FolderItemRoute)
    streams = routes.route(WorkspaceStreamRoute)
    stream_bindings = routes.route(WorkspaceStreamBindingRoute)
    stream_topics = routes.route(WorkspaceStreamTopicRoute)
    messages = routes.route(WorkspaceMessageRoute)
    drafts = routes.route(WorkspaceDraftRoute)
    external_accounts = routes.route(ExternalAccountRoute)
    external_chats = routes.route(ExternalChatRoute)
    external_operations = routes.route(ExternalOperationRoute)
    external_bridge_instances = routes.route(ExternalBridgeInstanceRoute)
    external_provider_policies = routes.route(ExternalProviderPolicyRoute)
    external_provider_health = routes.route(ExternalProviderHealthRoute)
    message_reactions = routes.route(WorkspaceMessageReactionRoute)
    files = routes.route(WorkspaceFileRoute)
    users = routes.route(WorkspaceUserRoute)
    me = routes.route(MeRoute)
