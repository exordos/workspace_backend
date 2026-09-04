# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import io
import json
import types
import uuid as sys_uuid
from unittest import mock

import pytest
import webob
from restalchemy.api import applications
from restalchemy.api import contexts as ra_contexts
from restalchemy.api import routes as ra_routes
from restalchemy.common import exceptions as ra_exceptions

from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.messenger_api import sticker_catalog
from workspace.messenger_api import sticker_import
from workspace.messenger_api.api import app as messenger_app
from workspace.messenger_api.api import sticker_controllers
from workspace.messenger_api.api import sticker_routes
from workspace.messenger_api.dm import stickers
from workspace.workspace_api.api import app as workspace_app
from workspace.workspace_api.api import routes as workspace_routes


USER_UUID = sys_uuid.UUID("10000000-0000-0000-0000-000000000001")
STICKER_UUID = sys_uuid.UUID("20000000-0000-0000-0000-000000000001")


def _request(path="/", method="GET", permissions=()):
    request = webob.Request.blank(path, method=method)
    request.api_context = ra_contexts.RequestContext(request)
    request.context = types.SimpleNamespace(
        user_uuid=USER_UUID,
        iam_context=types.SimpleNamespace(
            get_introspection_info=lambda: types.SimpleNamespace(
                permissions=list(permissions)
            )
        ),
    )
    return request


def _sticker(*, active=True, blocked=False):
    return stickers.Sticker(
        uuid=STICKER_UUID,
        title="Да",
        alt_text="Кивок",
        emoji=["👍"],
        tags=["да"],
        search_text="да кивок",
        category="gif",
        format="gif",
        width=None,
        height=180,
        size_bytes=5,
        sha256="a" * 64,
        media_object_id="stickers/private/media.gif",
        active=active,
        blocked=blocked,
    )


def _build_openapi(app_module):
    application = applications.OpenApiApplication(
        route_class=app_module.get_api_application(),
        openapi_engine=app_module.get_openapi_engine(),
    )
    request = webob.Request.blank("/specifications/3.0.3")
    request.application = application
    request.api_context = ra_contexts.RequestContext(request)
    return application.openapi_engine.build_openapi_specification("3.0.3", request)


def test_list_uses_only_explicit_query_contract_and_passthrough_response(monkeypatch):
    request = _request(
        f"/v1/stickers/?q=%D0%B4%D0%B0&favorite=true&uuid={STICKER_UUID}&page_limit=25"
    )
    controller = sticker_controllers.StickerController(request)
    session = object()
    repository = object()
    monkeypatch.setattr(controller, "_session", lambda: session)
    monkeypatch.setattr(controller, "_repository", lambda: repository)
    calls = []

    def list_stickers(*args, **kwargs):
        calls.append((args, kwargs))
        return sticker_catalog.StickerHttpResponse(
            body=None,
            status=304,
            headers={
                "ETag": '"etag"',
                "Cache-Control": "private, no-cache",
                "X-Pagination-Limit": "25",
            },
        )

    monkeypatch.setattr(sticker_catalog, "list_public_stickers", list_stickers)

    response = controller.do_collection()

    assert response.status_int == 304
    assert response.body == b""
    assert response.headers["ETag"] == '"etag"'
    assert calls == [
        (
            (session, USER_UUID, repository),
            {
                "q": "да",
                "favorite": True,
                "uuids": [STICKER_UUID],
                "category": None,
                "format": None,
                "page_limit": 25,
                "page_marker": None,
                "if_none_match": None,
            },
        )
    ]

    invalid = _request("/v1/stickers/?sort_key=uuid")
    with pytest.raises(ra_exceptions.ValidationErrorException):
        sticker_controllers.StickerController(invalid).do_collection()


def test_admin_update_checks_permission_before_body_and_returns_public_allowlist(
    monkeypatch,
):
    class BodySentinel:
        def read(self, *args, **kwargs):
            del args, kwargs
            raise AssertionError("body must not be read")

    unauthorized = _request(f"/v1/stickers/{STICKER_UUID}", method="PUT")
    unauthorized.environ["wsgi.input"] = BodySentinel()
    with pytest.raises(messenger_exceptions.ExternalResourceForbiddenError):
        sticker_controllers.StickerController(unauthorized).do_resource(
            str(STICKER_UUID)
        )

    authorized = _request(
        f"/v1/stickers/{STICKER_UUID}",
        method="PUT",
        permissions=(sticker_catalog.STICKER_CATALOG_MANAGE_PERMISSION,),
    )
    authorized.body = json.dumps({"title": "Новое название"}).encode()
    controller = sticker_controllers.StickerController(authorized)
    monkeypatch.setattr(controller, "_session", lambda: object())
    monkeypatch.setattr(controller, "_repository", lambda: object())
    monkeypatch.setattr(sticker_catalog, "update_sticker", lambda *args: _sticker())

    response = controller.do_resource(str(STICKER_UUID))
    payload = response.json

    assert set(payload) == {
        "id",
        "title",
        "alt_text",
        "emoji",
        "tags",
        "category",
        "media",
        "is_favorite",
    }
    assert set(payload["media"]) == {"format", "height", "url"}
    assert "media_object_id" not in response.text
    assert "private" not in response.text


@pytest.mark.parametrize("active,blocked", [(False, False), (False, True)])
def test_item_action_preload_uses_persisted_sticker_not_public_visibility(
    monkeypatch,
    active,
    blocked,
):
    expected = _sticker(active=active, blocked=blocked)
    get_one = mock.Mock(return_value=expected)
    monkeypatch.setattr(
        stickers.Sticker,
        "objects",
        types.SimpleNamespace(get_one=get_one),
    )

    actual = sticker_controllers.StickerController(_request()).get_resource_by_uuid(
        str(STICKER_UUID)
    )

    assert actual is expected
    get_one.assert_called_once_with(filters={"uuid": STICKER_UUID})


def test_download_action_preserves_raw_binary_response(monkeypatch):
    controller = sticker_controllers.StickerController(_request())
    monkeypatch.setattr(controller, "_session", lambda: object())
    monkeypatch.setattr(controller, "_repository", lambda: object())
    monkeypatch.setattr(controller, "_storage", lambda: object())
    monkeypatch.setattr(
        sticker_catalog,
        "download_sticker",
        lambda *args: sticker_catalog.StickerHttpResponse(
            body=b"GIF89a",
            status=200,
            headers={
                "Content-Type": "image/gif",
                "ETag": '"hash"',
                "Cache-Control": "private, max-age=31536000, immutable",
            },
        ),
    )

    response = controller.download._get(controller, _sticker())

    assert response.body == b"GIF89a"
    assert response.content_type == "image/gif"
    assert response.headers["ETag"] == '"hash"'


def test_collection_import_permission_precedes_multipart_access():
    class ParamsSentinel:
        @property
        def params(self):
            raise AssertionError("multipart params must not be read")

    class BodySentinel:
        def read(self, *args, **kwargs):
            del args, kwargs
            raise AssertionError("multipart body must not be read")

    request = _request("/actions/import_archive/invoke", method="POST")
    request.headers["Content-Type"] = "multipart/form-data; boundary=unused"
    request.api_context = ParamsSentinel()
    request.environ["wsgi.input"] = BodySentinel()

    with pytest.raises(messenger_exceptions.ExternalResourceForbiddenError):
        sticker_routes.StickerRoute(request).do()


def test_collection_import_passes_only_archive_file_to_service(monkeypatch):
    archive_file = io.BytesIO(b"zip")
    upload = types.SimpleNamespace(file=archive_file)
    request = _request(
        "/actions/import_archive/invoke",
        method="POST",
        permissions=(sticker_catalog.STICKER_CATALOG_MANAGE_PERMISSION,),
    )
    request.headers["Content-Type"] = "multipart/form-data; boundary=test"

    class ApiContext:
        params = {"archive": upload}

        def set_active_method(self, method):
            self.method = method

    request.api_context = ApiContext()
    session = object()
    repository = object()
    storage = object()
    monkeypatch.setattr(
        sticker_controllers.StickerController, "_session", lambda self: session
    )
    monkeypatch.setattr(
        sticker_controllers.StickerController,
        "_repository",
        lambda self: repository,
    )
    monkeypatch.setattr(
        sticker_controllers.StickerController, "_storage", lambda self: storage
    )
    calls = []

    class Result:
        @staticmethod
        def to_simple_type():
            return {"created": 0, "duplicates": 0, "items": []}

    def import_archive(source, *args):
        calls.append((source, args))
        return Result()

    monkeypatch.setattr(sticker_import, "import_archive", import_archive)

    response = sticker_routes.StickerRoute(request).do()

    assert response.status_int == 200
    assert response.json == {"created": 0, "duplicates": 0, "items": []}
    assert calls == [(archive_file, (session, USER_UUID, repository, storage))]


@pytest.mark.parametrize(
    ("method", "path", "error"),
    [
        ("GET", "/actions/import_archive/invoke", ra_exceptions.UnsupportedHttpMethod),
        ("POST", "/actions/import_archive", ra_exceptions.UnsupportedMethod),
    ],
)
def test_collection_import_rejects_wrong_method_or_missing_invoke(
    method,
    path,
    error,
):
    with pytest.raises(error):
        sticker_routes.StickerRoute(_request(path, method=method)).do()


def test_collection_import_is_not_an_item_action_and_fallback_is_unmodified():
    assert not hasattr(sticker_routes.StickerRoute, "import_archive")
    request = _request(f"/{STICKER_UUID}")
    original_path = request.path_info
    result = object()

    with mock.patch.object(ra_routes.Route, "do", return_value=result) as parent_do:
        assert sticker_routes.StickerRoute(request).do() is result

    assert request.path_info == original_path
    parent_do.assert_called_once_with(parent_resource=None)


def test_runtime_routes_mount_same_catalog_under_both_api_roots():
    assert messenger_app.MessengerApiApp.v1.stickers is sticker_routes.StickerRoute
    assert workspace_routes.MessengerRoute.stickers is sticker_routes.StickerRoute
    assert set(sticker_routes.StickerRoute.__allow_methods__) == {
        ra_routes.FILTER,
        ra_routes.GET,
        ra_routes.UPDATE,
    }
    assert sticker_routes.StickerRoute.download.is_invoke() is False
    assert sticker_routes.StickerRoute.star.is_invoke() is True
    assert sticker_routes.StickerRoute.unstar.is_invoke() is True


@pytest.mark.parametrize(
    ("application_route", "path"),
    [
        (messenger_app.MessengerApiApp, "/v1/stickers/"),
        (workspace_app.WorkspaceApiApp, "/v1/messenger/stickers/"),
    ],
)
def test_both_runtime_entrypoints_dispatch_catalog(
    monkeypatch,
    application_route,
    path,
):
    request = _request(path)
    monkeypatch.setattr(
        sticker_controllers.StickerController,
        "_session",
        lambda self: object(),
    )
    monkeypatch.setattr(
        sticker_controllers.StickerController,
        "_repository",
        lambda self: object(),
    )
    calls = []

    def list_stickers(*args, **kwargs):
        del args
        calls.append(kwargs)
        return sticker_catalog.StickerHttpResponse(
            body=b"[]",
            status=200,
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr(sticker_catalog, "list_public_stickers", list_stickers)

    response = application_route(request).do()

    assert response.status_int == 200
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("app_module", "root"),
    [
        (messenger_app, "/v1/"),
        (workspace_app, "/v1/messenger/"),
    ],
)
def test_openapi_exposes_exact_public_sticker_contract(app_module, root):
    specification = _build_openapi(app_module)
    paths = specification["paths"]
    item = f"{root}stickers/{{sticker_uuid}}"
    expected = {
        f"{root}stickers/",
        item,
        f"{item}/actions/download",
        f"{item}/actions/star/invoke",
        f"{item}/actions/unstar/invoke",
        f"{root}stickers/actions/import_archive/invoke",
    }
    actual = {path for path in paths if path.startswith(f"{root}stickers")}
    assert actual == expected
    assert set(paths[f"{root}stickers/"]) == {"get"}
    assert set(paths[item]) == {"get", "put"}
    assert {method for path in expected for method in paths[path]} <= {
        "get",
        "post",
        "put",
    }

    list_operation = paths[f"{root}stickers/"]["get"]
    parameters = {
        (value["in"], value["name"]): value for value in list_operation["parameters"]
    }
    assert set(parameters) == {
        ("query", "q"),
        ("query", "favorite"),
        ("query", "uuid"),
        ("query", "category"),
        ("query", "format"),
        ("query", "page_limit"),
        ("query", "page_marker"),
        ("header", "If-None-Match"),
    }
    assert parameters[("query", "page_marker")]["schema"] == {"type": "string"}
    assert "filter" not in {name for _, name in parameters}
    assert set(list_operation["responses"]) == {200, 304}
    assert set(list_operation["responses"][200]["headers"]) == {
        "ETag",
        "Cache-Control",
        "X-Pagination-Limit",
        "X-Pagination-Marker",
    }

    for path in expected:
        for operation in paths[path].values():
            assert operation["security"] == [{"bearerAuth": []}]
    update = paths[item]["put"]
    assert update["x-required-permission"] == "workspace.sticker_catalog.manage"
    assert set(
        update["requestBody"]["content"]["application/json"]["schema"]["properties"]
    ) == {"title", "alt_text", "emoji", "tags", "category", "active", "blocked"}
    import_operation = paths[f"{root}stickers/actions/import_archive/invoke"]["post"]
    multipart = import_operation["requestBody"]["content"]
    assert set(multipart) == {"multipart/form-data"}
    assert multipart["multipart/form-data"]["schema"] == {
        "type": "object",
        "required": ["archive"],
        "additionalProperties": False,
        "properties": {"archive": {"type": "string", "format": "binary"}},
    }
    assert import_operation["x-required-permission"] == (
        "workspace.sticker_catalog.manage"
    )

    public_schema = specification["components"]["schemas"]["StickerCard"]
    properties = public_schema["properties"]
    for internal in (
        "uuid",
        "search_text",
        "sha256",
        "size_bytes",
        "media_object_id",
        "created_at",
        "updated_at",
    ):
        assert internal not in properties
    assert "Sticker_Get" not in specification["components"]["schemas"]
    assert "Sticker_Update" not in specification["components"]["schemas"]
