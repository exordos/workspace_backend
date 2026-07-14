# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from restalchemy.api import routes

from workspace.groupware.api import controllers


class MailMessageSendAction(routes.Action):
    __controller__ = controllers.MailMessageController


class MailMessageMoveAction(routes.Action):
    __controller__ = controllers.MailMessageController


class MailAttachmentDownloadAction(routes.Action):
    __controller__ = controllers.MailAttachmentController


class CalendarEventMoveAction(routes.Action):
    __controller__ = controllers.CalendarEventController


class MailFolderRoute(routes.Route):
    __controller__ = controllers.MailFolderController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.UPDATE,
        routes.DELETE,
    ]


class MailMessageRoute(routes.Route):
    __controller__ = controllers.MailMessageController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.UPDATE,
        routes.DELETE,
    ]

    send = routes.action(MailMessageSendAction, invoke=True)
    move = routes.action(MailMessageMoveAction, invoke=True)


class MailAttachmentRoute(routes.Route):
    __controller__ = controllers.MailAttachmentController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.DELETE,
    ]

    download = routes.action(MailAttachmentDownloadAction)


class CalendarRoute(routes.Route):
    __controller__ = controllers.CalendarController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.UPDATE,
        routes.DELETE,
    ]


class CalendarEventRoute(routes.Route):
    __controller__ = controllers.CalendarEventController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.UPDATE,
        routes.DELETE,
    ]

    move = routes.action(CalendarEventMoveAction, invoke=True)


class MailRoute(routes.Route):
    __controller__ = controllers.MailFolderController
    __allow_methods__ = []

    folders = routes.route(MailFolderRoute)
    messages = routes.route(MailMessageRoute)
    attachments = routes.route(MailAttachmentRoute)


class CalendarApiRoute(routes.Route):
    __controller__ = controllers.CalendarController
    __allow_methods__ = []

    calendars = routes.route(CalendarRoute)
    events = routes.route(CalendarEventRoute)
