# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

from restalchemy.api import routes

from workspace.calendar_api import controllers


class CalendarRoute(routes.Route):
    __controller__ = controllers.CalendarController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.UPDATE,
        routes.DELETE,
    ]


class CalendarEventMoveAction(routes.Action):
    __controller__ = controllers.CalendarEventController


class CalendarEventRoute(CalendarRoute):
    __controller__ = controllers.CalendarEventController
    move = routes.action(CalendarEventMoveAction, invoke=True)
