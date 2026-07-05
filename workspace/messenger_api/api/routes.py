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


class ExternalAccountRoute(routes.Route):
    __controller__ = controllers.ExternalAccountController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.UPDATE,
    ]


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

    def do(self, parent_resource=None, **kwargs):
        controller = self.get_controller(request=self._req)
        return controller.toggle_done.do_post(
            controller=controller,
            resource=parent_resource,
        )


class WorkspaceStreamTopicNotificationsAction(routes.Action):
    __controller__ = controllers.WorkspaceStreamTopicController


class WorkspaceStreamTopicReadAction(routes.Action):
    __controller__ = controllers.WorkspaceStreamTopicController


class WorkspaceUserPresenceAction(routes.Action):
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
    read = routes.action(WorkspaceStreamTopicReadAction, invoke=True)


class WorkspaceUserRoute(routes.Route):
    __controller__ = controllers.WorkspaceUserController
    __allow_methods__ = [
        routes.FILTER,
        routes.GET,
    ]

    presence = routes.action(WorkspaceUserPresenceAction, invoke=True)


class MeRoute(routes.Route):
    """Handler for /v1/me/ endpoint."""

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
    message_reactions = routes.route(WorkspaceMessageReactionRoute)
    files = routes.route(WorkspaceFileRoute)
    external_accounts = routes.route(ExternalAccountRoute)
    events = routes.route(WorkspaceEventRoute)
    epoch = routes.route(WorkspaceEpochRoute)
    users = routes.route(WorkspaceUserRoute)
    me = routes.route(MeRoute)
