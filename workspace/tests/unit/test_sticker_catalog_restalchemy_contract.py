# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import inspect

import pytest
import webob
from restalchemy.api import actions as ra_actions
from restalchemy.api import contexts as ra_contexts
from restalchemy.api import routes as ra_routes

from workspace.messenger_api.api import routes as messenger_routes


def test_item_action_conventions_match_existing_messenger_routes():
    """Existing routes define the contract for sticker item actions."""
    assert messenger_routes.WorkspaceFileRoute.download.is_invoke() is False
    assert messenger_routes.WorkspaceMessageRoute.star.is_invoke() is True
    assert messenger_routes.WorkspaceMessageRoute.unstar.is_invoke() is True


def test_item_crud_methods_are_enabled_for_catalog_route_shape():
    """A sticker item route must opt into GET and PUT like existing resources."""
    assert messenger_routes.WorkspaceFileRoute.check_allow_methods(
        ra_routes.GET,
        ra_routes.UPDATE,
    )


def test_stock_route_does_not_dispatch_collection_actions():
    """RestAlchemy's stock dispatcher expects actions after a resource UUID."""

    class ResourceLookupCalled(Exception):
        pass

    class CollectionController:
        def __init__(self, request):
            self.request = request

        def get_resource_by_uuid(self, *args, **kwargs):
            del args, kwargs
            raise ResourceLookupCalled()

        @staticmethod
        def import_archive(*args, **kwargs):
            del args, kwargs

    class ImportAction(ra_routes.Action):
        __controller__ = CollectionController

    class CollectionRoute(ra_routes.Route):
        __controller__ = CollectionController
        __allow_methods__ = [ra_routes.FILTER]
        import_archive = ra_routes.action(ImportAction, invoke=True)

    request = webob.Request.blank(
        "/stickers/actions/import_archive/invoke",
        method="POST",
    )
    with pytest.raises(ResourceLookupCalled):
        CollectionRoute(request).do()

    source = inspect.getsource(ra_routes.Route.do)
    assert "get_resource_by_uuid(name, parent_resource)" in source


def test_collection_action_route_override_dispatches_the_selected_shape():
    """The project route keeps RestAlchemy Action method/invoke validation."""

    class CollectionController:
        def __init__(self, request):
            self.request = request

        @staticmethod
        def process_result(result, *args, **kwargs):
            del args, kwargs
            return result

        @ra_actions.post
        def import_archive(self, resource, **kwargs):
            assert resource is None
            assert kwargs == {}
            return {"status": "called"}

    class ImportAction(ra_routes.Action):
        __controller__ = CollectionController

    class CollectionActionRoute(ra_routes.Route):
        __controller__ = CollectionController
        __allow_methods__ = [ra_routes.FILTER]
        import_archive = ra_routes.action(ImportAction, invoke=True)

        def do(self, **kwargs):
            name, path = self._req.path_info_pop(), self._req.path_info_peek()
            if name == "actions" and path is not None:
                action_name = path
                action = self.get_action(action_name)(self._req)
                return action.do(resource=None, **kwargs)
            return super().do(**kwargs)

    request = webob.Request.blank(
        "/actions/import_archive/invoke",
        method="POST",
    )
    request.api_context = ra_contexts.RequestContext(request)

    assert CollectionActionRoute(request).do() == {"status": "called"}
