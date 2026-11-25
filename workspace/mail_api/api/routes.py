# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from restalchemy.api import routes

from workspace.mail_api.api import controllers


class MailMessageSendAction(routes.Action):
    __controller__ = controllers.MailDraftController


class MailMessageAction(routes.Action):
    __controller__ = controllers.MailMessageController


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
        routes.FILTER,
        routes.GET,
        routes.DELETE,
    ]

    move = routes.action(MailMessageAction, invoke=True)
    read = routes.action(MailMessageAction, invoke=True)
    unread = routes.action(MailMessageAction, invoke=True)
    star = routes.action(MailMessageAction, invoke=True)
    unstar = routes.action(MailMessageAction, invoke=True)
    trash = routes.action(MailMessageAction, invoke=True)
    restore = routes.action(MailMessageAction, invoke=True)


class MailDraftRoute(routes.Route):
    __controller__ = controllers.MailDraftController
    __allow_methods__ = [
        routes.CREATE,
        routes.FILTER,
        routes.GET,
        routes.UPDATE,
        routes.DELETE,
    ]

    send = routes.action(MailMessageSendAction, invoke=True)
