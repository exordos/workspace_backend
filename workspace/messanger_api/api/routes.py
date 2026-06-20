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

from workspace.messanger_api.api import controllers


class FolderItemPinAction(routes.Action):
    __controller__ = controllers.FolderItemController


class FolderItemUnpinAction(routes.Action):
    __controller__ = controllers.FolderItemController


class FolderItemRoute(routes.Route):
    __controller__ = controllers.FolderItemController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.UPDATE,
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

    items = routes.route(FolderItemRoute, resource_route=True)


class FolderItemsRoute(routes.Route):
    __controller__ = controllers.FolderItemsController
    __allow_methods__ = [
        routes.FILTER,
    ]


class ApiEndpointRoute(routes.Route):
    """Handler for /v1/ endpoint."""

    __controller__ = controllers.ApiEndpointController
    __allow_methods__ = [routes.FILTER]

    folders = routes.route(FolderRoute)
    folder_items = routes.route(FolderItemsRoute)
