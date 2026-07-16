# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from restalchemy.api import routes

from workspace.messenger_api.api import routes as messenger_routes
from workspace.user_api.api import routes as user_routes
from workspace.workspace_api.api import controllers


class MessengerRoute(routes.Route):
    __controller__ = controllers.MessengerApiEndpointController
    __allow_methods__ = [routes.FILTER]

    folders = routes.route(messenger_routes.FolderRoute)
    folder_items = routes.route(messenger_routes.FolderItemRoute)
    streams = routes.route(messenger_routes.WorkspaceStreamRoute)
    stream_bindings = routes.route(messenger_routes.WorkspaceStreamBindingRoute)
    stream_topics = routes.route(messenger_routes.WorkspaceStreamTopicRoute)
    messages = routes.route(messenger_routes.WorkspaceMessageRoute)
    message_reactions = routes.route(
        messenger_routes.WorkspaceMessageReactionRoute,
    )
    files = routes.route(messenger_routes.WorkspaceFileRoute)


class ServiceRoute(user_routes.ServiceRoute):
    __allow_methods__ = [routes.FILTER, routes.GET]


class WorkspaceApiEndpointRoute(routes.Route):
    __controller__ = controllers.WorkspaceApiEndpointController
    __allow_methods__ = [routes.FILTER]

    users = routes.route(messenger_routes.WorkspaceUserRoute)
    services = routes.route(ServiceRoute)
    me = routes.route(messenger_routes.MeRoute)
    events = routes.route(messenger_routes.WorkspaceEventRoute)
    epoch = routes.route(messenger_routes.WorkspaceEpochRoute)
    messenger = routes.route(MessengerRoute)
