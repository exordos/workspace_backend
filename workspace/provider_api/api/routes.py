# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from restalchemy.api import routes

from workspace.provider_api.api import controllers


class ExternalAccountStatusAction(routes.Action):
    __controller__ = controllers.ProviderExternalAccountController


class MailCommandResultAction(routes.Action):
    __controller__ = controllers.ProviderMailCommandController


class ProviderBlobDownloadAction(routes.Action):
    __controller__ = controllers.ProviderBlobController


class CalendarCommandResultAction(routes.Action):
    __controller__ = controllers.ProviderCalendarCommandController


class MessengerCommandResultAction(routes.Action):
    __controller__ = controllers.ProviderMessengerCommandController


class MessengerMessageFlagsAction(routes.Action):
    __controller__ = controllers.ProviderMessengerMessageController


class ProviderExternalAccountRoute(routes.Route):
    __controller__ = controllers.ProviderExternalAccountController
    __allow_methods__ = [routes.FILTER, routes.GET]

    status = routes.action(ExternalAccountStatusAction, invoke=True)


class ProviderBlobRoute(routes.Route):
    __controller__ = controllers.ProviderBlobController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.DELETE,
    ]

    download = routes.action(ProviderBlobDownloadAction)


class ProviderMailFolderRoute(routes.Route):
    __controller__ = controllers.ProviderMailFolderController
    __allow_methods__ = [routes.FILTER, routes.GET, routes.UPDATE, routes.DELETE]


class ProviderMailMessageRoute(routes.Route):
    __controller__ = controllers.ProviderMailMessageController
    __allow_methods__ = [routes.FILTER, routes.GET, routes.UPDATE, routes.DELETE]


class ProviderMailCommandRoute(routes.Route):
    __controller__ = controllers.ProviderMailCommandController
    __allow_methods__ = [routes.FILTER, routes.GET]

    result = routes.action(MailCommandResultAction, invoke=True)


class ProviderMailRoute(routes.Route):
    __controller__ = controllers.ProviderMailEndpointController
    __allow_methods__ = []

    folders = routes.route(ProviderMailFolderRoute)
    messages = routes.route(ProviderMailMessageRoute)
    commands = routes.route(ProviderMailCommandRoute)


class ProviderCalendarRouteResource(routes.Route):
    __controller__ = controllers.ProviderCalendarController
    __allow_methods__ = [routes.FILTER, routes.GET, routes.UPDATE, routes.DELETE]


class ProviderCalendarEventRoute(routes.Route):
    __controller__ = controllers.ProviderCalendarEventController
    __allow_methods__ = [routes.FILTER, routes.GET, routes.UPDATE, routes.DELETE]


class ProviderCalendarCommandRoute(routes.Route):
    __controller__ = controllers.ProviderCalendarCommandController
    __allow_methods__ = [routes.FILTER, routes.GET]

    result = routes.action(CalendarCommandResultAction, invoke=True)


class ProviderCalendarRoute(routes.Route):
    __controller__ = controllers.ProviderCalendarEndpointController
    __allow_methods__ = []

    calendars = routes.route(ProviderCalendarRouteResource)
    events = routes.route(ProviderCalendarEventRoute)
    commands = routes.route(ProviderCalendarCommandRoute)


class ProviderMessengerUserRoute(routes.Route):
    __controller__ = controllers.ProviderMessengerUserController
    __allow_methods__ = [routes.FILTER, routes.GET, routes.UPDATE, routes.DELETE]


class ProviderMessengerStreamRoute(routes.Route):
    __controller__ = controllers.ProviderMessengerStreamController
    __allow_methods__ = [routes.FILTER, routes.GET, routes.UPDATE, routes.DELETE]


class ProviderMessengerTopicRoute(routes.Route):
    __controller__ = controllers.ProviderMessengerTopicController
    __allow_methods__ = [routes.FILTER, routes.GET, routes.UPDATE, routes.DELETE]


class ProviderMessengerMessageRoute(routes.Route):
    __controller__ = controllers.ProviderMessengerMessageController
    __allow_methods__ = [routes.FILTER, routes.GET, routes.UPDATE, routes.DELETE]

    flags = routes.action(MessengerMessageFlagsAction, invoke=True)


class ProviderMessengerReactionRoute(routes.Route):
    __controller__ = controllers.ProviderMessengerReactionController
    __allow_methods__ = [routes.FILTER, routes.GET, routes.UPDATE, routes.DELETE]


class ProviderMessengerCommandRoute(routes.Route):
    __controller__ = controllers.ProviderMessengerCommandController
    __allow_methods__ = [routes.FILTER, routes.GET]

    result = routes.action(MessengerCommandResultAction, invoke=True)


class ProviderMessengerRoute(routes.Route):
    __controller__ = controllers.ProviderMessengerEndpointController
    __allow_methods__ = []

    users = routes.route(ProviderMessengerUserRoute)
    streams = routes.route(ProviderMessengerStreamRoute)
    topics = routes.route(ProviderMessengerTopicRoute)
    messages = routes.route(ProviderMessengerMessageRoute)
    reactions = routes.route(ProviderMessengerReactionRoute)
    commands = routes.route(ProviderMessengerCommandRoute)


class ProviderRoute(routes.Route):
    __controller__ = controllers.ProviderController
    __allow_methods__ = [routes.FILTER, routes.GET, routes.UPDATE]

    external_accounts = routes.route(
        ProviderExternalAccountRoute,
        resource_route=True,
    )
    blobs = routes.route(ProviderBlobRoute, resource_route=True)
    mail = routes.route(ProviderMailRoute, resource_route=True)
    calendar = routes.route(ProviderCalendarRoute, resource_route=True)
    messenger = routes.route(ProviderMessengerRoute, resource_route=True)


class ProviderApiEndpointRoute(routes.Route):
    __controller__ = controllers.ProviderApiEndpointController
    __allow_methods__ = [routes.FILTER]

    providers = routes.route(ProviderRoute)
