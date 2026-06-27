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
    ]

    add_users = routes.action(WorkspaceStreamBindingsAction, invoke=True)


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


class WorkspaceStreamTopicToggleDoneAction(routes.Action):
    __controller__ = controllers.WorkspaceStreamTopicController


class WorkspaceStreamTopicRoute(routes.Route):
    __controller__ = controllers.WorkspaceStreamTopicController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.UPDATE,
        routes.DELETE,
    ]

    toggle_done = routes.action(WorkspaceStreamTopicToggleDoneAction, invoke=True)


class WorkspaceUserRoute(routes.Route):
    __controller__ = controllers.WorkspaceUserController
    __allow_methods__ = [
        routes.FILTER,
        routes.GET,
    ]


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
    events = routes.route(WorkspaceEventRoute)
    epoch = routes.route(WorkspaceEpochRoute)
    users = routes.route(WorkspaceUserRoute)
    me = routes.route(MeRoute)
