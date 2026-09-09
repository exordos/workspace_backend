# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import types

import pytest
import webob
from restalchemy.api import actions as ra_actions
from restalchemy.api import contexts as ra_contexts
from restalchemy.api import routes as ra_routes
from restalchemy.common import contexts as ra_common_contexts
from restalchemy.common import exceptions as ra_exceptions

from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.messenger_api.api import permission_guards
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


def test_collection_action_pre_auth_rejects_before_params_or_body_access():
    """Unauthorized multipart requests are rejected before Action.do parsing."""

    class ParamsSentinel:
        @property
        def params(self):
            raise AssertionError("request params must not be read")

    class BodySentinel:
        def read(self, *args, **kwargs):
            del args, kwargs
            raise AssertionError("request body must not be read")

    class CollectionController:
        def __init__(self, request):
            self.request = request

        @staticmethod
        def process_result(result, *args, **kwargs):
            del args, kwargs
            return result

        @ra_actions.post
        def import_archive(self, resource, **kwargs):
            del resource, kwargs
            raise AssertionError("unauthorized action must not run")

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
                request_parts = self._req.path_info.strip("/").split("/")
                if self._req.method != ra_routes.POST:
                    raise ra_exceptions.UnsupportedHttpMethod(
                        method=self._req.method,
                    )
                if len(request_parts) != 2 or request_parts[1] != "invoke":
                    raise ra_exceptions.UnsupportedMethod(
                        method=request_parts[-1],
                        object_name=action_name,
                    )
                permission_guards.require_iam_permission(
                    self._req.context,
                    "workspace.sticker_catalog.manage",
                )
                action = self.get_action(action_name)(self._req)
                return action.do(resource=None, **kwargs)
            return super().do(**kwargs)

    request = webob.Request.blank(
        "/actions/import_archive/invoke",
        method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=unused"},
    )
    request.api_context = ParamsSentinel()
    request.context = types.SimpleNamespace(
        iam_context=types.SimpleNamespace(
            get_introspection_info=lambda: types.SimpleNamespace(permissions=[]),
        ),
    )
    request.environ["wsgi.input"] = BodySentinel()

    with pytest.raises(messenger_exceptions.ExternalResourceForbiddenError):
        CollectionActionRoute(request).do()


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/actions/import_archive/invoke", ra_exceptions.UnsupportedHttpMethod),
        ("POST", "/actions/import_archive", ra_exceptions.UnsupportedMethod),
    ],
)
def test_collection_action_guard_rejects_non_post_invoke_forms(method, path, expected):
    """Collection import has one explicit POST + invoke shape."""

    class GuardRoute:
        def __init__(self, request):
            self._req = request

        def do(self):
            name, action_path = self._req.path_info_pop(), self._req.path_info_peek()
            assert name == "actions"
            request_parts = self._req.path_info.strip("/").split("/")
            if self._req.method != ra_routes.POST:
                raise ra_exceptions.UnsupportedHttpMethod(method=self._req.method)
            if len(request_parts) != 2 or request_parts[1] != "invoke":
                raise ra_exceptions.UnsupportedMethod(
                    method=request_parts[-1],
                    object_name=action_path,
                )

    request = webob.Request.blank(path, method=method)
    request.context = types.SimpleNamespace(
        iam_context=types.SimpleNamespace(
            get_introspection_info=lambda: types.SimpleNamespace(permissions=[]),
        ),
    )

    with pytest.raises(expected):
        GuardRoute(request).do()


def test_restalchemy_commits_a_normal_500_response_and_rolls_back_exceptions():
    """A returned error response is normal exit; exceptions trigger rollback."""

    class Session:
        def __init__(self):
            self.commits = 0
            self.rollbacks = 0

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    context = ra_common_contexts.Context()
    session = Session()
    context.start_new_session = lambda: session
    context.session_close = lambda: None

    with context.session_manager() as active_session:
        assert active_session is session
        returned_status = 500

    assert returned_status == 500
    assert session.commits == 1
    assert session.rollbacks == 0

    with pytest.raises(RuntimeError):
        with context.session_manager():
            raise RuntimeError("handler failure")

    assert session.commits == 1
    assert session.rollbacks == 1
