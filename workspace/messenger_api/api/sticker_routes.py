# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import typing

from restalchemy.api import routes
from restalchemy.common import exceptions as ra_exceptions

from workspace.messenger_api import sticker_catalog
from workspace.messenger_api.api import permission_guards
from workspace.messenger_api.api import sticker_controllers


class StickerDownloadAction(routes.Action):
    __controller__ = sticker_controllers.StickerController


class StickerStarAction(routes.Action):
    __controller__ = sticker_controllers.StickerController


class StickerUnstarAction(routes.Action):
    __controller__ = sticker_controllers.StickerController


class StickerImportArchiveAction(routes.Action):
    __controller__ = sticker_controllers.StickerController


StickerImportArchiveActionRoute = routes.action(
    StickerImportArchiveAction,
    invoke=True,
)


class StickerRoute(routes.Route):
    """Sticker resource plus the one explicit collection command."""

    __controller__ = sticker_controllers.StickerController
    __allow_methods__ = [routes.FILTER, routes.GET, routes.UPDATE]

    download = routes.action(StickerDownloadAction)
    star = routes.action(StickerStarAction, invoke=True)
    unstar = routes.action(StickerUnstarAction, invoke=True)

    def do(
        self,
        parent_resource: typing.Any = None,
        **kwargs: typing.Any,
    ) -> typing.Any:
        if self._req.path_info_peek() != "actions":
            return super().do(parent_resource=parent_resource, **kwargs)

        self._req.path_info_pop()
        action_name = self._req.path_info_peek()
        request_parts = self._req.path_info.strip("/").split("/")
        if self._req.method != routes.POST:
            raise ra_exceptions.UnsupportedHttpMethod(method=self._req.method)
        if (
            action_name != "import_archive"
            or len(request_parts) != 2
            or request_parts[1] != "invoke"
        ):
            raise ra_exceptions.UnsupportedMethod(
                method=request_parts[-1] if request_parts else None,
                object_name=action_name,
            )
        permission_guards.require_iam_permission(
            self._req.context,
            sticker_catalog.STICKER_CATALOG_MANAGE_PERMISSION,
        )
        return StickerImportArchiveActionRoute(self._req).do(
            resource=None,
            **kwargs,
        )
